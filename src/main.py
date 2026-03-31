import argparse

from lm_studio_health import ensure_lm_studio_ready


def run_cli_chatbot() -> None:
    from agent import run_agent

    print("\n" + "=" * 60)
    print("Grafana CHATBOT - CLI MODE")
    print("Type a question and press Enter.")
    print("Type 'exit', 'quit', or 'q' to stop.")
    print(
        "Tip: run 'python3.11 src/main.py ingest-docs --force' to refresh docs cache/index."
    )
    print("=" * 60)

    while True:
        question = input("\nAsk something: ").strip()
        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            break

        result = run_agent(question, allow_actions=True)
        print(f"\nAssistant:\n{result['answer']}")
        reflection = result.get("reflection", {})
        print(
            "\nAgent Evaluation: "
            f"groundedness={reflection.get('groundedness_score', 'n/a')}/5, "
            f"completeness={reflection.get('completeness_score', 'n/a')}/5, "
            f"actionability={reflection.get('actionability_score', 'n/a')}/5"
        )


def run_ingestion(max_pages: int, force: bool) -> None:
    from grafana_docs_ingest import ingest_grafana_docs

    print("[1/2] Checking LM Studio availability...")
    ensure_lm_studio_ready()
    print("[2/2] Ingesting knowledge context...")
    stats = ingest_grafana_docs(max_pages=max_pages, force=force)
    dashboard_files = int(stats.get("dashboard_files", 0) or 0)
    if stats.get("from_cache"):
        if stats.get("index_reused"):
            print(
                "Done. Using cached context + existing vector index. "
                f"Pages: {stats['pages']}, dashboards: {dashboard_files}, records: {stats['records']}"
            )
        else:
            print(
                "Done. Using cached context and rebuilt vector index. "
                f"Pages: {stats['pages']}, dashboards: {dashboard_files}, records: {stats['records']}"
            )
    else:
        print(
            "Done. Context indexed. "
            f"Pages: {stats['pages']}, dashboards: {dashboard_files}, records: {stats['records']}"
        )


def run_auto_ingestion(max_pages: int) -> None:
    from grafana_docs_ingest import ingest_grafana_docs

    print("[Startup] Ensuring knowledge index...")
    stats = ingest_grafana_docs(max_pages=max_pages, force=False)
    dashboard_files = int(stats.get("dashboard_files", 0) or 0)
    if stats.get("from_cache"):
        if stats.get("index_reused"):
            print(
                "[Startup] Using cached context + existing vector index: "
                f"pages={stats['pages']}, dashboards={dashboard_files}, records={stats['records']}"
            )
        else:
            print(
                "[Startup] Using cached context and rebuilt vector index: "
                f"pages={stats['pages']}, dashboards={dashboard_files}, records={stats['records']}"
            )
    else:
        print(
            "[Startup] Indexed context: "
            f"pages={stats['pages']}, dashboards={dashboard_files}, records={stats['records']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agentic DevOps chatbot",
        epilog="Default mode is 'web' if no subcommand is provided.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="Docs pages to crawl on first run (0 means all reachable pages)",
    )
    subparsers = parser.add_subparsers(dest="mode")

    ingest_parser = subparsers.add_parser(
        "ingest-docs", help="Fetch and index Grafana documentation"
    )
    ingest_parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="Maximum docs pages to crawl (0 means all)",
    )
    ingest_parser.add_argument(
        "--force",
        action="store_true",
        help="Force recrawl/reindex even if cache exists",
    )

    web_parser = subparsers.add_parser("web", help="Run web interface")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host for web server")
    web_parser.add_argument(
        "--port", type=int, default=8000, help="Port for web server"
    )
    web_parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="Docs pages to crawl on first run (0 means all)",
    )

    cli_parser = subparsers.add_parser("cli", help="Run CLI chat interface")
    cli_parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="Docs pages to crawl on first run (0 means all)",
    )
    return parser.parse_args()


def _close_vector_store_safely() -> None:
    try:
        from vector_store import close_client

        close_client()
    except Exception:
        pass


def main() -> None:
    args = parse_args()
    mode = args.mode or "web"
    max_pages = getattr(args, "max_pages", 40)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    try:
        ensure_lm_studio_ready()
        if mode == "ingest-docs":
            run_ingestion(max_pages=max_pages, force=args.force)
        elif mode == "cli":
            run_auto_ingestion(max_pages=max_pages)
            run_cli_chatbot()
        elif mode == "web":
            from web_ui import run_web_server

            run_auto_ingestion(max_pages=max_pages)
            run_web_server(host=host, port=port)
        else:
            raise ValueError(f"Unsupported mode: {mode}")
    except RuntimeError as exc:
        print(f"Error: {exc}")
    finally:
        _close_vector_store_safely()


if __name__ == "__main__":
    main()
