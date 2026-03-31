import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent import run_agent

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "data" / "output"
LEGACY_OUTPUT_DIR = BASE_DIR / "data" / "output"
TEMPLATE_FILE = BASE_DIR / "templates" / "chat.html"


def _latest_dashboard_from_disk() -> str:
    candidates: list[Path] = []
    for output_dir in (OUTPUT_DIR, LEGACY_OUTPUT_DIR):
        if not output_dir.exists():
            continue
        candidates.extend(
            p for p in output_dir.glob("*.dashboard.json") if p.exists() and p.is_file()
        )
    if not candidates:
        return ""
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].name


def _render_page() -> str:
    if not TEMPLATE_FILE.exists():
        raise RuntimeError(f"Missing template file: {TEMPLATE_FILE}")
    return TEMPLATE_FILE.read_text()


def run_web_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    class Handler(BaseHTTPRequestHandler):
        latest_dashboard_file: str = ""

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/download":
                query = parse_qs(parsed.query)
                name = query.get("name", [""])[0]
                safe_name = Path(name).name
                if not safe_name:
                    self.send_response(400)
                    self.end_headers()
                    return
                candidate_paths = [
                    OUTPUT_DIR / safe_name,
                    LEGACY_OUTPUT_DIR / safe_name,
                ]
                file_path = next(
                    (p for p in candidate_paths if p.exists() and p.is_file()), None
                )
                if file_path is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{safe_name}"'
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            body = _render_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.path != "/api/ask":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON payload"}, status=400)
                return

            question = str(payload.get("question", "")).strip()
            if not question:
                self._send_json({"error": "Question is required"}, status=400)
                return

            latest_dashboard_file = str(
                payload.get("latest_dashboard_file", "")
            ).strip()
            if not latest_dashboard_file:
                latest_dashboard_file = Handler.latest_dashboard_file
            if not latest_dashboard_file:
                latest_dashboard_file = _latest_dashboard_from_disk()
            session_context = (
                {"latest_dashboard_file": latest_dashboard_file}
                if latest_dashboard_file
                else {}
            )
            result = run_agent(
                question, allow_actions=True, session_context=session_context
            )
            answer = result.get("answer", "")
            reflection = result.get("reflection", {})
            artifacts = result.get("artifacts", [])
            if isinstance(artifacts, list):
                for item in reversed(artifacts):
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("type", "")).strip() != "dashboard_json":
                        continue
                    name = str(item.get("name", "")).strip()
                    if not name:
                        continue
                    Handler.latest_dashboard_file = Path(name).name
                    break
            self._send_json(
                {"answer": answer, "reflection": reflection, "artifacts": artifacts}
            )

    server = HTTPServer((host, port), Handler)
    print(f"Web UI running at http://{host}:{port} (HTTP only)")
    try:
        server.serve_forever()
    finally:
        server.server_close()
