"""Base constants and shared helpers for toolkit functions."""

import ast

import difflib

import json

import os

import re

import ssl

from pathlib import Path

from typing import Any

from urllib.parse import urlencode

from urllib.request import Request, urlopen

import certifi

# toolkit_backend/base.py -> project root is 2 levels up from parent dir.
BASE_DIR = Path(__file__).resolve().parents[2]

OUTPUT_DIR = BASE_DIR / "data" / "output"
LEGACY_OUTPUT_DIR = BASE_DIR / "src" / "data" / "output"

CACHE_CHUNKS_FILE = BASE_DIR / "data" / "cache" / "grafana_docs_chunks.json"

MAX_REPO_FILE_BYTES = 250_000

REPO_TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".mmd",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".html",
    ".js",
    ".ts",
    ".css",
    ".ini",
}

REPO_SPECIAL_FILES = {
    "README",
    "README.md",
    "requirements.txt",
    "Dockerfile",
    ".gitignore",
}

REPO_EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
}

REPO_EXCLUDED_PREFIXES = (
    "data/",
    "data/raw/",
    "data/cache/qdrant_db/",
)

VENDOR_TERMS = ("azure", "aws", "gcp", "google cloud", "cloudwatch", "azure monitor")

COMMON_TERMS = {
    "what",
    "when",
    "where",
    "which",
    "how",
    "create",
    "build",
    "make",
    "monitor",
    "using",
    "use",
    "with",
    "into",
    "from",
    "that",
    "this",
    "grafana",
    "dashboard",
    "cluster",
}

BOILERPLATE_PHRASES = (
    "was this page helpful",
    "suggest an edit in github",
    "create a github issue",
    "related resources from grafana labs",
    "video getting started with",
)

PROMETHEUS_BASE_URL = os.getenv("PROMETHEUS_BASE_URL", "http://127.0.0.1:9090").rstrip(
    "/"
)

PROMETHEUS_TIMEOUT_SEC = float(os.getenv("PROMETHEUS_TIMEOUT_SEC", "4"))

PROMETHEUS_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _safe_output_filename(filename: str) -> str:
    safe_name = Path(str(filename or "")).name
    if not safe_name:
        raise ValueError("filename is required")
    return safe_name


def _resolve_output_file_path(
    filename: str, migrate_legacy_to_canonical: bool = False
) -> Path:
    safe_name = _safe_output_filename(filename)
    canonical_path = OUTPUT_DIR / safe_name
    legacy_path = LEGACY_OUTPUT_DIR / safe_name

    if canonical_path.exists() and canonical_path.is_file():
        return canonical_path

    if legacy_path.exists() and legacy_path.is_file():
        if migrate_legacy_to_canonical:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            canonical_path.write_bytes(legacy_path.read_bytes())
            return canonical_path
        return legacy_path

    return canonical_path


def _is_safe_repo_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(BASE_DIR.resolve())
        return True
    except Exception:
        return False


def _iter_repository_files() -> list[Path]:
    files: list[Path] = []
    base_resolved = BASE_DIR.resolve()
    excluded_prefix_roots = [prefix.rstrip("/") for prefix in REPO_EXCLUDED_PREFIXES]

    for root, dirnames, filenames in os.walk(base_resolved, topdown=True):
        root_path = Path(root)
        try:
            root_rel = root_path.resolve().relative_to(base_resolved).as_posix()
        except Exception:
            continue

        pruned_dirs: list[str] = []
        for dirname in dirnames:
            if dirname in REPO_EXCLUDED_DIRS:
                continue
            rel_dir = dirname if root_rel == "." else f"{root_rel}/{dirname}"
            if any(rel_dir.startswith(prefix) for prefix in excluded_prefix_roots):
                continue
            pruned_dirs.append(dirname)
        dirnames[:] = pruned_dirs

        for filename in filenames:
            file_path = root_path / filename
            try:
                rel = file_path.resolve().relative_to(base_resolved).as_posix()
            except Exception:
                continue
            if any(rel.startswith(prefix) for prefix in excluded_prefix_roots):
                continue
            if (
                file_path.suffix.lower() not in REPO_TEXT_EXTENSIONS
                and file_path.name not in REPO_SPECIAL_FILES
            ):
                continue
            try:
                if file_path.stat().st_size > MAX_REPO_FILE_BYTES:
                    continue
            except Exception:
                continue
            files.append(file_path)
    files.sort(key=lambda p: p.as_posix())
    return files


def _render_line_snippet(lines: list[str], line_idx: int, radius: int = 1) -> str:
    start = max(0, line_idx - radius)
    end = min(len(lines), line_idx + radius + 1)
    rendered: list[str] = []
    for idx in range(start, end):
        rendered.append(f"{idx + 1}: {lines[idx]}")
    return "\n".join(rendered)


def _query_terms_for_repo(query: str) -> list[str]:
    focus = _focus_terms(query)
    if focus:
        return focus
    return _query_terms(query)


def _query_mentions_vendor(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in VENDOR_TERMS)


def _is_vendor_specific_chunk(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in VENDOR_TERMS)


def _query_terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) > 2]


def _focus_terms(query: str) -> list[str]:
    return [t for t in _query_terms(query) if t not in COMMON_TERMS]


def _chunk_relevance_score(text: str, query: str) -> int:
    lowered = text.lower()
    terms = _query_terms(query)
    focus = _focus_terms(query)
    if not terms:
        return 0

    term_hits = sum(lowered.count(term) for term in terms)
    unique_hits = sum(1 for term in set(terms) if term in lowered)
    focus_hits = sum(1 for term in set(focus) if term in lowered)
    score = (focus_hits * 6) + (unique_hits * 2) + term_hits

    if focus and focus_hits == 0:
        score -= 10
    if any(phrase in lowered for phrase in BOILERPLATE_PHRASES):
        score -= 10
    return score


def _rerank_and_filter_chunks(chunks: list[str], query: str, top_k: int) -> list[str]:
    scored: list[tuple[int, str]] = []
    for chunk in chunks:
        score = _chunk_relevance_score(chunk, query)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


def _http_get_json(
    url: str, timeout_seconds: float = PROMETHEUS_TIMEOUT_SEC
) -> dict[str, Any]:
    request = Request(
        url=url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(
        request,
        timeout=max(1.0, float(timeout_seconds)),
        context=PROMETHEUS_SSL_CONTEXT,
    ) as response:
        payload = response.read().decode("utf-8", errors="ignore")
    parsed = json.loads(payload)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("Expected JSON object.")


PROMQL_RESERVED_TOKENS = {
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "bool",
    "or",
    "and",
    "unless",
    "offset",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "quantile",
    "stddev",
    "stdvar",
    "topk",
    "bottomk",
    "count_values",
    "rate",
    "irate",
    "increase",
    "delta",
    "idelta",
    "deriv",
    "predict_linear",
    "holt_winters",
    "time",
    "vector",
    "scalar",
    "clamp",
    "clamp_min",
    "clamp_max",
    "abs",
    "absent",
    "round",
    "floor",
    "ceil",
    "ln",
    "log2",
    "log10",
    "sqrt",
    "exp",
    "histogram_quantile",
}


def _extract_metric_names_from_expr(expr: str) -> list[str]:
    text = str(expr or "")
    tokens = re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", text)
    metrics: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in PROMQL_RESERVED_TOKENS:
            continue
        if token.startswith("__"):
            continue
        if (
            ":" in token
            and not token.startswith("container_")
            and not token.startswith("kube_")
        ):
            # Drop most recording-rule style labels unless they are known metric prefixes.
            continue
        if token not in metrics:
            metrics.append(token)
    return metrics


def _replace_metric_in_expr(expr: str, old_metric: str, new_metric: str) -> str:
    pattern = re.compile(rf"(?<![a-zA-Z0-9_:]){re.escape(old_metric)}(?![a-zA-Z0-9_:])")
    return pattern.sub(new_metric, expr)


PROMQL_METRIC_FALLBACKS: dict[str, list[str]] = {
    "container_memory_working_set_bytes": ["container_memory_usage_bytes"],
    "container_cpu_usage_seconds_total": [
        "container_cpu_user_seconds_total",
        "container_cpu_system_seconds_total",
    ],
    "kube_pod_container_status_restarts_total": [
        "kube_pod_container_status_last_terminated_reason"
    ],
}
