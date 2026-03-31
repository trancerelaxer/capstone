# Agentic Grafana Assistant

AI-agentic assistant that:
- ingests Grafana documentation and public Kubernetes dashboard examples into a local RAG index,
- analyzes this repository/codebase on demand,
- generates Grafana assets (dashboards, alerts, prompt templates),
- runs dashboard quality gates (PromQL lint + Grafana JSON lint with optional auto-fix),
- probes Prometheus metric availability and adapts dashboard queries when metrics are missing,
- patches existing dashboard JSON files and emits a unified diff artifact,
- evaluates response quality with a repeatable benchmark script.

## Architecture

- Diagram: `architecture.mmd`
- Knowledge sources:
  - `https://grafana.com/docs/grafana/latest/`
  - `https://github.com/dotdc/grafana-dashboards-kubernetes` (`dashboards/*.json`)
- Ingestion behavior:
  - first run: fetch + chunk + embed + index
  - next runs: reuse cached metadata/chunks

## Project Structure

```text
/
├── README.md
├── architecture.mmd
├── requirements.txt
├── data/
│   ├── raw/grafana_docs/      # cached raw HTML pages from docs crawl
│   ├── raw/grafana_dashboards/# cached raw JSON from dotdc dashboards repo
│   ├── cache/
│   │   ├── grafana_docs_chunks.json
│   │   ├── grafana_docs_meta.json
│   │   ├── embedding_cache.json
│   │   └── qdrant_db/
│   └── output/                # generated dashboards/alerts/prompts + chat history
└── src/
    ├── main.py                # entrypoint: ingest-docs | web | cli
    ├── grafana_docs_ingest.py # docs crawler + chunking + indexing
    ├── web_ui.py              # HTTP chat interface with history + downloads
    ├── agent.py               # unified agent  (run + tool exports + constants)
    ├── runtime_engine/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── spec_inference.py
    │   ├── planner.py
    │   ├── pipeline.py
    │   └── runner.py
    ├── grafana_toolkit.py     # toolkit (public API)
    ├── toolkit_frontend/
    │   ├── __init__.py
    │   ├── retrieval.py       # retrieval/read/calc/write tool exports
    │   ├── generation.py      # dashboard/alert/prompt generation exports
    │   └── quality.py         # probe/lint/patch tool exports
    ├── toolkit_backend/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── retrieval.py
    │   ├── promql.py
    │   ├── generation.py
    │   ├── dashboard_lint_base.py
    │   ├── dashboard_lint.py
    │   └── dashboard_patch.py
    ├── evaluate_agent.py      # benchmark runner + objective scoring report
    ├── embeddings.py          # LM Studio embeddings + cache
    ├── vector_store.py        # Qdrant vector storage
    └── lm_studio_health.py    # LM Studio health-check
```

## Technologies / Libraries

- `openai` (LM Studio OpenAI-compatible APIs)
- `qdrant-client` (vector database)
- `numpy` (vector handling)
- `urllib` (HTTP crawling with stdlib)
- `certifi` (TLS CA bundle for HTTPS crawl)

## Installation

```bash
pip install -r requirements.txt
```

## Environment Variables

```bash
export LM_STUDIO_BASE_URL="http://localhost:1234/v1"
export LM_STUDIO_API_KEY="lm-studio"
export LM_STUDIO_EMBEDDING_MODEL="text-embedding-nomic-embed-text-v1.5"
export LM_STUDIO_CHAT_MODEL="google/gemma-3-4b"
```

## Run

1. Start LM Studio server and load one embedding + one chat model.

2. Start web mode (default). On first run it crawls docs and builds the index:

```bash
python3.11 src/main.py
```

3. Open `http://127.0.0.1:8000` (HTTP only)

Optional explicit modes:

```bash
# force recrawl/reindex
python3.11 src/main.py ingest-docs --max-pages 40 --force

# CLI mode
python3.11 src/main.py cli --max-pages 40
```

## What You Can Ask

- Repository/codebase analysis:
  - "Check the created code and explain what we are building."
  - "Which files implement tool-calling and self-reflection?"
  - "How does `/api/ask` work end-to-end?"
- Documentation help:
  - "How to create Grafana alert rules for Prometheus?"
  - "What are best practices for dashboard variables?"
- Generate reusable files:
  - "Create a dashboard JSON for API latency and error rate."
  - "Generate a Grafana alert JSON for high 5xx error rate."
  - "Create a prompt template JSON for incident triage."
  - "Fix this dashboard and keep no templating variables."
  - "Check if these Prometheus metrics exist: kube_pod_status_phase, container_memory_working_set_bytes."

Generated files appear in `data/output/` and are downloadable from the web chat.

## Evaluation (Capstone Evidence)

Run a fixed benchmark to produce a measurable JSON report:

```bash
python3.11 src/evaluate_agent.py

# dashboard-generation golden suite
python3.11 src/evaluate_agent.py --suite dashboard
```

Output:
- `data/output/agent_evaluation_report.json`
- `data/output/dashboard_golden_report.json`
- pass rate + average groundedness/completeness/actionability scores
