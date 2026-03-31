import json
import re
import ssl
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen

import certifi

from embeddings import EMBEDDING_MODEL, embed_texts
from vector_store import get_collection_status, init_collection, insert_chunks

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw" / "grafana_docs"
DASHBOARD_RAW_DIR = BASE_DIR / "data" / "raw" / "grafana_dashboards"
CACHE_DIR = BASE_DIR / "data" / "cache"
CHUNKS_FILE = CACHE_DIR / "grafana_docs_chunks.json"
META_FILE = CACHE_DIR / "grafana_docs_meta.json"

DOCS_ROOT = "https://grafana.com/docs/grafana/latest/"
SITEMAP_ROOT = "https://grafana.com/sitemap.xml"
DASHBOARD_REPO = "dotdc/grafana-dashboards-kubernetes"
DASHBOARD_REPO_API_ROOT = f"https://api.github.com/repos/{DASHBOARD_REPO}"
DASHBOARD_REPO_RAW_ROOT = f"https://raw.githubusercontent.com/{DASHBOARD_REPO}"
DASHBOARD_REPO_DASHBOARDS_PREFIX = "dashboards/"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
INGESTION_VERSION = 3


def _http_get_text(url: str, accept: str = "text/html") -> str:
    request = Request(
        url=url, headers={"User-Agent": "Mozilla/5.0", "Accept": accept}, method="GET"
    )
    with urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
        return response.read().decode("utf-8", errors="ignore")


def _http_get_json(url: str) -> dict:
    payload = _http_get_text(url, accept="application/vnd.github+json")
    parsed = json.loads(payload)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("Expected JSON object.")


def _extract_xml_locs(xml_text: str) -> list[str]:
    return [
        m.strip()
        for m in re.findall(r"<loc>(.*?)</loc>", xml_text, flags=re.IGNORECASE)
    ]


def _fetch_sitemap_urls(max_depth: int = 3) -> list[str]:
    """Discover docs pages from sitemap(s) for reliable full-site ingestion."""
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(SITEMAP_ROOT, 0)])
    page_urls: set[str] = set()

    while queue:
        url, depth = queue.popleft()
        if url in visited or depth > max_depth:
            continue
        visited.add(url)
        try:
            xml_text = _http_get_text(url)
        except Exception:
            continue

        for loc in _extract_xml_locs(xml_text):
            clean, _ = urldefrag(loc)
            if clean.startswith(DOCS_ROOT):
                page_urls.add(clean)
                continue
            if "sitemap" in clean and clean not in visited:
                queue.append((clean, depth + 1))

    return sorted(page_urls)


def _extract_links(html: str, page_url: str) -> list[str]:
    links = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    normalized: list[str] = []
    for href in links:
        absolute = urljoin(page_url, href)
        absolute, _ = urldefrag(absolute)
        if absolute.startswith(DOCS_ROOT):
            normalized.append(absolute)
    return normalized


def _html_to_text(html: str) -> str:
    # Prefer content area to avoid nav/footer noise that hurts retrieval relevance.
    main_match = re.search(r"<main[^>]*>([\s\S]*?)</main>", html, flags=re.IGNORECASE)
    if main_match:
        html = main_match.group(1)
    else:
        article_match = re.search(
            r"<article[^>]*>([\s\S]*?)</article>", html, flags=re.IGNORECASE
        )
        if article_match:
            html = article_match.group(1)

    without_scripts = re.sub(
        r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE
    )
    without_styles = re.sub(
        r"<style[\s\S]*?</style>", " ", without_scripts, flags=re.IGNORECASE
    )
    stripped = re.sub(r"<[^>]+>", " ", without_styles)
    text = re.sub(r"\s+", " ", stripped).strip()
    # Remove common documentation footer boilerplate.
    for marker in [
        "Was this page helpful?",
        "Suggest an edit in GitHub",
        "Create a GitHub issue",
        "Email docs@grafana.com",
        "Related resources from Grafana Labs",
    ]:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx].strip()
    return text


def _chunk_text(text: str, chunk_size: int = 1400, overlap: int = 200) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    step = max(1, chunk_size - overlap)
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += step
    return chunks


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _collect_dashboard_panels(panels: list) -> list[dict]:
    if not isinstance(panels, list):
        return []
    collected: list[dict] = []
    queue: deque = deque(panels)
    while queue:
        panel = queue.popleft()
        if not isinstance(panel, dict):
            continue
        collected.append(panel)
        nested = panel.get("panels")
        if isinstance(nested, list):
            queue.extend(nested)
    return collected


def _extract_target_query(target: dict) -> str:
    if not isinstance(target, dict):
        return ""
    for key in ("expr", "query", "rawSql", "rawQuery", "jql"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value)
    return ""


def _dashboard_json_to_text(
    path: str, payload: dict, raw_url: str, view_url: str
) -> str:
    dashboard = (
        payload.get("dashboard")
        if isinstance(payload.get("dashboard"), dict)
        else payload
    )
    if not isinstance(dashboard, dict):
        return ""

    title = _one_line(dashboard.get("title", ""))
    uid = _one_line(dashboard.get("uid", ""))
    description = _one_line(dashboard.get("description", ""))

    tags_raw = dashboard.get("tags", [])
    tags = (
        [str(t).strip() for t in tags_raw if str(t).strip()]
        if isinstance(tags_raw, list)
        else []
    )

    variable_names: list[str] = []
    templating = dashboard.get("templating")
    if isinstance(templating, dict):
        for variable in templating.get("list", []):
            if not isinstance(variable, dict):
                continue
            name = _one_line(variable.get("name", ""))
            if name:
                variable_names.append(name)

    lines: list[str] = [
        f"Repository: {DASHBOARD_REPO}",
        f"Dashboard path: {path}",
        f"Dashboard URL: {view_url}",
        f"Raw JSON URL: {raw_url}",
    ]
    if title:
        lines.append(f"Dashboard title: {title}")
    if uid:
        lines.append(f"Dashboard uid: {uid}")
    if tags:
        lines.append(f"Dashboard tags: {', '.join(tags)}")
    if variable_names:
        lines.append(f"Variables: {', '.join(variable_names)}")
    if description:
        lines.append(f"Description: {description}")

    panels = _collect_dashboard_panels(dashboard.get("panels", []))
    if panels:
        lines.append(f"Panel count: {len(panels)}")

    for index, panel in enumerate(panels, start=1):
        panel_title = _one_line(panel.get("title", "")) or f"Panel {index}"
        panel_type = _one_line(panel.get("type", ""))
        panel_line = f"Panel {index}: {panel_title}"
        if panel_type:
            panel_line += f" [type={panel_type}]"
        lines.append(panel_line)

        targets = panel.get("targets", [])
        if not isinstance(targets, list):
            continue

        for target in targets:
            if not isinstance(target, dict):
                continue
            query = _extract_target_query(target)
            if not query:
                continue
            ref_id = _one_line(target.get("refId", "")) or "?"
            lines.append(f"Target {ref_id}: {query}")

    return "\n".join(lines).strip()


def _fetch_dashboard_repo_pages() -> list[dict]:
    DASHBOARD_RAW_DIR.mkdir(parents=True, exist_ok=True)

    try:
        repo_meta = _http_get_json(DASHBOARD_REPO_API_ROOT)
    except Exception:
        return []

    default_branch = _one_line(repo_meta.get("default_branch", "")) or "main"
    tree_url = f"{DASHBOARD_REPO_API_ROOT}/git/trees/{default_branch}?recursive=1"
    try:
        tree_payload = _http_get_json(tree_url)
    except Exception:
        return []

    tree_items = tree_payload.get("tree", [])
    if not isinstance(tree_items, list):
        return []

    dashboard_paths = sorted(
        item.get("path", "")
        for item in tree_items
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and str(item.get("path", "")).startswith(DASHBOARD_REPO_DASHBOARDS_PREFIX)
        and str(item.get("path", "")).endswith(".json")
    )

    pages: list[dict] = []
    for path in dashboard_paths:
        raw_url = f"{DASHBOARD_REPO_RAW_ROOT}/{default_branch}/{path}"
        view_url = f"https://github.com/{DASHBOARD_REPO}/blob/{default_branch}/{path}"
        try:
            raw_json = _http_get_text(raw_url, accept="application/json")
            parsed = json.loads(raw_json)
        except Exception:
            continue

        safe_name = path.replace("/", "__")
        (DASHBOARD_RAW_DIR / safe_name).write_text(raw_json)
        text = _dashboard_json_to_text(
            path=path, payload=parsed, raw_url=raw_url, view_url=view_url
        )
        if not text:
            continue

        pages.append({"url": view_url, "path": path, "text": text})

    return pages


def _crawl_docs(max_pages: int) -> list[dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    visited: set[str] = set()
    queue: deque[str] = deque([DOCS_ROOT])
    pages: list[dict] = []

    unlimited = max_pages <= 0
    while queue and (unlimited or len(visited) < max_pages):
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            html = _http_get_text(url)
        except Exception:
            continue

        parsed = urlparse(url)
        safe_name = parsed.path.strip("/").replace("/", "__") or "index"
        (RAW_DIR / f"{safe_name}.html").write_text(html)

        text = _html_to_text(html)
        pages.append({"url": url, "text": text})

        for link in _extract_links(html, url):
            if link not in visited and link not in queue:
                queue.append(link)
    return pages


def _collect_docs_urls(max_pages: int) -> list[str]:
    """Prefer sitemap-based discovery; fallback to HTML crawl if needed."""
    sitemap_urls = _fetch_sitemap_urls()
    if sitemap_urls:
        if max_pages <= 0:
            return sitemap_urls
        return sitemap_urls[:max_pages]

    # Fallback: discover URLs via crawl from root.
    crawled_pages = _crawl_docs(max_pages=max_pages)
    return [p["url"] for p in crawled_pages]


def _fetch_pages_from_urls(urls: list[str]) -> list[dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    pages: list[dict] = []
    for url in urls:
        try:
            html = _http_get_text(url)
        except Exception:
            continue

        parsed = urlparse(url)
        safe_name = parsed.path.strip("/").replace("/", "__") or "index"
        (RAW_DIR / f"{safe_name}.html").write_text(html)
        pages.append({"url": url, "text": _html_to_text(html)})
    return pages


def _index_records(records: list[dict], reset: bool = True) -> None:
    if not records:
        return
    chunks = [str(r.get("text", "")) for r in records]
    metadatas = [r.get("metadata", {}) for r in records]
    embeddings = embed_texts(chunks)
    vector_size = len(embeddings[0]) if embeddings else 384
    init_collection(vector_size=vector_size, reset=reset)
    insert_chunks(chunks, embeddings, metadatas=metadatas)


def ingest_grafana_docs(max_pages: int = 40, force: bool = False) -> dict[str, int]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not force and META_FILE.exists() and CHUNKS_FILE.exists():
        try:
            cached = json.loads(META_FILE.read_text())
        except Exception:
            cached = {}
        cached_pages = int(cached.get("pages", 0) or 0)
        cached_dashboard_files = int(cached.get("dashboard_files", 0) or 0)
        cached_records_count = int(cached.get("records", 0) or 0)
        cached_embedding_model = str(cached.get("embedding_model", "")).strip()
        cached_ingestion_version = int(cached.get("ingestion_version", 0) or 0)
        cached_max_pages = int(cached.get("max_pages", max_pages) or max_pages)
        # Guard against stale/partial cache from earlier failed runs.
        has_cached_content = (cached_pages > 1) or (cached_dashboard_files > 0)
        if (
            has_cached_content
            and cached_records_count > 0
            and cached_ingestion_version == INGESTION_VERSION
        ):
            collection = get_collection_status()
            collection_ready = (
                bool(collection.get("exists"))
                and int(collection.get("points", 0)) >= cached_records_count
                and int(collection.get("vector_size") or 0) > 0
            )
            model_matches = (not cached_embedding_model) or (
                cached_embedding_model == EMBEDDING_MODEL
            )

            if collection_ready and model_matches:
                return {
                    "pages": cached_pages,
                    "dashboard_files": cached_dashboard_files,
                    "records": cached_records_count,
                    "from_cache": 1,
                    "index_reused": 1,
                }

            try:
                cached_records = json.loads(CHUNKS_FILE.read_text())
            except Exception:
                cached_records = []
            if isinstance(cached_records, list) and cached_records:
                _index_records(cached_records, reset=True)
                return {
                    "pages": cached_pages,
                    "dashboard_files": cached_dashboard_files,
                    "records": cached_records_count,
                    "from_cache": 1,
                    "index_reused": 0,
                }
        # If we need a cache refresh because ingestion logic changed, keep previous scope.
        if (
            cached_ingestion_version != INGESTION_VERSION
            and max_pages == 40
            and cached_max_pages != 40
        ):
            max_pages = cached_max_pages

    urls = _collect_docs_urls(max_pages=max_pages)
    pages = _fetch_pages_from_urls(urls)
    dashboard_pages = _fetch_dashboard_repo_pages()

    if not pages and not dashboard_pages:
        return {"pages": 0, "dashboard_files": 0, "records": 0, "from_cache": 0}

    records: list[dict] = []
    for page in pages:
        url = page["url"]
        text = page["text"]
        for idx, chunk in enumerate(_chunk_text(text)):
            records.append(
                {
                    "text": chunk,
                    "metadata": {
                        "source": "Grafana Docs",
                        "doc_url": url,
                        "chunk_index": idx,
                    },
                }
            )

    for dashboard_page in dashboard_pages:
        url = dashboard_page["url"]
        path = dashboard_page["path"]
        text = dashboard_page["text"]
        for idx, chunk in enumerate(_chunk_text(text)):
            records.append(
                {
                    "text": chunk,
                    "metadata": {
                        "source": DASHBOARD_REPO,
                        "doc_url": url,
                        "doc_path": path,
                        "chunk_index": idx,
                    },
                }
            )

    CHUNKS_FILE.write_text(json.dumps(records, ensure_ascii=True, indent=2))
    META_FILE.write_text(
        json.dumps(
            {
                "pages": len(pages),
                "dashboard_files": len(dashboard_pages),
                "records": len(records),
                "docs_root": DOCS_ROOT,
                "dashboard_repo": DASHBOARD_REPO,
                "max_pages": max_pages,
                "embedding_model": EMBEDDING_MODEL,
                "ingestion_version": INGESTION_VERSION,
            },
            ensure_ascii=True,
            indent=2,
        )
    )

    _index_records(records, reset=True)

    return {
        "pages": len(pages),
        "dashboard_files": len(dashboard_pages),
        "records": len(records),
        "from_cache": 0,
    }
