"""RAG/repository retrieval and utility tool implementations."""

from toolkit_backend.base import *  # noqa: F401,F403
from toolkit_backend.base import (  # noqa: F401
    _chunk_relevance_score,
    _is_safe_repo_path,
    _is_vendor_specific_chunk,
    _iter_repository_files,
    _query_mentions_vendor,
    _query_terms,
    _query_terms_for_repo,
    _render_line_snippet,
    _rerank_and_filter_chunks,
)


def retrieve_context(query: str, top_k: int = 10) -> dict:
    query = str(query or "").strip()
    try:
        top_k_int = int(top_k)
    except Exception:
        top_k_int = 10
    top_k_int = max(1, min(top_k_int, 20))
    if not query:
        query = "grafana dashboard kubernetes monitoring"

    chunks: list[str] = []
    retrieval_mode = "vector"
    try:
        from embeddings import embed_query
        from vector_store import search

        query_vector = embed_query(query)
        # Overfetch and rerank lexically to reduce noisy/boilerplate chunks.
        vector_top_k = max(top_k_int * 4, 8)
        chunks = search(query_vector, top_k=vector_top_k)
        chunks = _rerank_and_filter_chunks(chunks, query=query, top_k=top_k_int)
    except Exception:
        chunks = []

    if not chunks:
        retrieval_mode = "keyword_fallback"
        chunks = _keyword_fallback_search(query, top_k=top_k_int)
    elif not _query_mentions_vendor(query):
        # Prefer provider-neutral docs unless user explicitly asked for a cloud vendor.
        neutral_chunks = [
            chunk for chunk in chunks if not _is_vendor_specific_chunk(chunk)
        ]
        if neutral_chunks:
            chunks = neutral_chunks[:top_k_int]

    return {
        "query": query,
        "top_k": top_k_int,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "retrieval_mode": retrieval_mode,
    }


def _keyword_fallback_search(query: str, top_k: int = 10) -> list[str]:
    if not CACHE_CHUNKS_FILE.exists():
        return []

    try:
        records = json.loads(CACHE_CHUNKS_FILE.read_text())
    except Exception:
        return []

    if not isinstance(records, list):
        return []

    query_terms = _query_terms(query)
    if not query_terms:
        return []

    wants_vendor = _query_mentions_vendor(query)
    scored: list[tuple[int, str, str]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        if not text:
            continue
        score = _chunk_relevance_score(text, query)
        if not wants_vendor and _is_vendor_specific_chunk(text):
            score -= 5
        if score > 0:
            metadata = item.get("metadata", {})
            source = (
                str(metadata.get("source", "")).strip()
                if isinstance(metadata, dict)
                else ""
            )
            scored.append((score, text, source))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    rendered: list[str] = []
    for _, text, source in top:
        source_label = source or "Grafana Docs"
        rendered.append(f"[Source: {source_label}] {text}")
    return rendered


def retrieve_repo_context(query: str, top_k: int = 8) -> dict:
    query = str(query or "").strip()
    if not query:
        query = "project overview and architecture"
    try:
        top_k_int = int(top_k)
    except Exception:
        top_k_int = 8
    top_k_int = max(1, min(top_k_int, 20))

    query_terms = _query_terms_for_repo(query)
    explicit_paths = re.findall(
        r"([a-zA-Z0-9_./-]+\.(?:py|md|mmd|json|yaml|yml|toml|txt|html|js|ts|css))",
        query,
    )
    normalized_paths = [Path(p).as_posix().lstrip("./").lower() for p in explicit_paths]
    candidates: list[tuple[int, str]] = []
    for path in _iter_repository_files():
        rel = path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
        rel_lower = rel.lower()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        lines = text.splitlines()
        line_hits = 0
        path_boost = 0
        if normalized_paths and any(rel_lower.endswith(p) for p in normalized_paths):
            path_boost = 12
        for idx, line in enumerate(lines):
            lowered = line.lower()
            score = sum(lowered.count(term) for term in query_terms)
            if query.lower() in lowered:
                score += 3
            if any(term in rel_lower for term in query_terms):
                score += 1
            score += path_boost
            if score <= 0:
                continue
            line_hits += 1
            if line_hits > 3:
                break
            snippet = _render_line_snippet(lines, idx, radius=1)
            candidates.append((score, f"[Repo Source: {rel}:{idx + 1}]\n{snippet}"))

    retrieval_mode = "repo_keyword"
    if not candidates:
        fallback_paths = [
            BASE_DIR / "README.md",
            BASE_DIR / "architecture.mmd",
            BASE_DIR / "src" / "main.py",
            BASE_DIR / "src" / "agent.py",
        ]
        chunks: list[str] = []
        for fallback_path in fallback_paths:
            if not fallback_path.exists() or not fallback_path.is_file():
                continue
            try:
                rel = fallback_path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
                lines = fallback_path.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
            except Exception:
                continue
            snippet = "\n".join(
                [f"{idx + 1}: {line}" for idx, line in enumerate(lines[:30])]
            )
            chunks.append(f"[Repo Source: {rel}:1]\n{snippet}")
            if len(chunks) >= top_k_int:
                break
        return {
            "query": query,
            "top_k": top_k_int,
            "chunk_count": len(chunks),
            "chunks": chunks,
            "retrieval_mode": "repo_fallback",
        }

    candidates.sort(key=lambda x: x[0], reverse=True)
    unique_chunks: list[str] = []
    seen: set[str] = set()
    for _, chunk in candidates:
        if chunk in seen:
            continue
        seen.add(chunk)
        unique_chunks.append(chunk)
        if len(unique_chunks) >= top_k_int:
            break
    return {
        "query": query,
        "top_k": top_k_int,
        "chunk_count": len(unique_chunks),
        "chunks": unique_chunks,
        "retrieval_mode": retrieval_mode,
    }


def read_repo_file(path: str, start_line: int = 1, end_line: int = 200) -> dict:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("path is required")

    candidate = Path(raw_path)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (BASE_DIR / candidate).resolve()
    )
    if not _is_safe_repo_path(resolved):
        raise ValueError("Path must be inside the project repository")
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"File not found: {raw_path}")

    try:
        start = int(start_line)
    except Exception:
        start = 1
    try:
        end = int(end_line)
    except Exception:
        end = 200
    start = max(1, start)
    end = max(start, min(end, start + 400))

    lines = resolved.read_text(encoding="utf-8", errors="ignore").splitlines()
    selected = lines[start - 1 : end]
    rendered = "\n".join(
        [f"{idx}: {line}" for idx, line in enumerate(selected, start=start)]
    )
    rel = resolved.relative_to(BASE_DIR.resolve()).as_posix()
    chunk = f"[Repo Source: {rel}:{start}]\n{rendered}" if rendered else ""
    return {
        "path": rel,
        "start_line": start,
        "end_line": end,
        "line_count": len(selected),
        "content": rendered,
        "chunks": [chunk] if chunk else [],
    }


def safe_calculate(expression: str) -> dict:
    allowed_ops = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.Pow: lambda a, b: a**b,
        ast.Mod: lambda a, b: a % b,
    }

    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = _eval(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_ops:
            left = _eval(node.left)
            right = _eval(node.right)
            return allowed_ops[type(node.op)](left, right)
        raise ValueError("Unsupported expression.")

    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval(tree.body)
        return {"expression": expression, "result": value}
    except Exception as exc:
        return {"expression": expression, "error": str(exc)}


def write_brief(filename: str, content: str) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    if not safe_name.endswith(".md"):
        safe_name = f"{safe_name}.md"
    path = OUTPUT_DIR / safe_name
    path.write_text(content)
    return {"saved_to": str(path), "bytes": len(content.encode("utf-8"))}


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "grafana-dashboard"


def _upsert_label_filter(expr: str, label: str, value: str, operator: str = "=") -> str:
    filter_part = f'{label}{operator}"{value}"'
    selector_match = re.search(r"\{([^{}]*)\}", expr)
    if selector_match:
        inner = selector_match.group(1).strip()
        parts = (
            [part.strip() for part in inner.split(",") if part.strip()] if inner else []
        )
        replaced = False
        matcher_pattern = re.compile(
            rf"^{re.escape(label)}\s*(=|=~|!=|!~)\s*(['\"]).*?\2$"
        )
        for idx, part in enumerate(parts):
            if matcher_pattern.match(part):
                parts[idx] = filter_part
                replaced = True
                break
        if not replaced:
            parts.append(filter_part)
        new_inner = ",".join(parts)
        return f"{expr[:selector_match.start()]}{{{new_inner}}}{expr[selector_match.end():]}"
    return expr


def _add_template_filters(
    expr: str, variable_filters: dict[str, str] | None = None
) -> str:
    filtered = expr
    lowered = expr.lower()
    if isinstance(variable_filters, dict) and variable_filters:
        for label, variable_value in variable_filters.items():
            if re.search(rf"\b{re.escape(label.lower())}\b", lowered):
                filtered = _upsert_label_filter(
                    filtered, label=label, value=variable_value, operator="=~"
                )
        return filtered

    # Default non-template safety filters (always on for generated dashboards).
    # This keeps generated PromQL practical without forcing Grafana templating variables.
    default_filters = {
        "namespace": ("!=", ""),
        "pod": ("!=", ""),
        "container": ("!=", ""),
    }
    for label, (operator, value) in default_filters.items():
        if re.search(rf"\b{re.escape(label)}\b", lowered):
            filtered = _upsert_label_filter(
                filtered, label=label, value=value, operator=operator
            )
    return filtered


def _normalize_promql(expr: str, variable_filters: dict[str, str] | None = None) -> str:
    normalized = str(expr or "").strip()
    if not normalized:
        return "up"

    normalized = (
        normalized.replace("`", "")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
    )
    normalized = re.sub(r"\s+", " ", normalized)

    def _colon_selector_repl(match: re.Match[str]) -> str:
        label = match.group(1)
        value = match.group(2)
        if value == "*":
            return f'{label}=~".+"'
        return f'{label}="{value}"'

    normalized = re.sub(
        r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*"([^"]*)"', _colon_selector_repl, normalized
    )
    normalized = re.sub(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"\*"', r'\1=~".+"', normalized)
    normalized = re.sub(
        r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=|=~|!=|!~)\s*'([^']*)'",
        lambda m: f'{m.group(1)}{m.group(2)}"{m.group(3)}"',
        normalized,
    )

    where_match = re.search(
        r"\bwhere\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([a-zA-Z0-9_\"'./-]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if where_match:
        label = where_match.group(1)
        value = where_match.group(2).strip("\"'")
        if label.lower() == "phase":
            value = value.capitalize()
        normalized = normalized[: where_match.start()].strip()
        normalized = _upsert_label_filter(normalized, label, value)

    lowered = normalized.lower()
    if "select " in lowered or " from " in lowered:
        return "up"

    normalized = _add_template_filters(normalized, variable_filters=variable_filters)
    return normalized or "up"


ALLOWED_PANEL_TYPES = {"timeseries", "stat", "barchart"}
