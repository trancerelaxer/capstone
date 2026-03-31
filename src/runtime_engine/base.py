"""Runtime shared constants, tools registry, and low-level helpers."""

import json

import os

import re

from typing import Any

from openai import OpenAI

from agent import (
    add_panel_to_dashboard,
    check_prometheus_metrics,
    generate_grafana_alert_rule,
    generate_grafana_dashboard,
    generate_prompt_template,
    lint_grafana_dashboard,
    patch_grafana_dashboard,
    read_repo_file,
    retrieve_context,
    retrieve_repo_context,
    safe_calculate,
    write_brief,
)

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")

LM_STUDIO_API_KEY = os.getenv("LM_STUDIO_API_KEY", "lm-studio")

LM_STUDIO_CHAT_MODEL = os.getenv("LM_STUDIO_CHAT_MODEL", "openai/gpt-oss-20b")

client = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key=LM_STUDIO_API_KEY)

MAX_CONTEXT_CHARS = int(os.getenv("AGENT_MAX_CONTEXT_CHARS", "2200"))

MAX_TOOL_SUMMARY_CHARS = int(os.getenv("AGENT_MAX_TOOL_SUMMARY_CHARS", "700"))

MAX_QUESTION_CHARS = int(os.getenv("AGENT_MAX_QUESTION_CHARS", "500"))

MAX_REFLECT_CONTEXT_CHARS = int(os.getenv("AGENT_MAX_REFLECT_CONTEXT_CHARS", "1200"))

MAX_REFLECT_ANSWER_CHARS = int(os.getenv("AGENT_MAX_REFLECT_ANSWER_CHARS", "1200"))

DEFAULT_TOP_K = int(os.getenv("AGENT_DEFAULT_TOP_K", "3"))

VENDOR_TERMS = ("azure", "aws", "gcp", "google cloud")

TOOLS = {
    "retrieve_context": retrieve_context,
    "retrieve_repo_context": retrieve_repo_context,
    "read_repo_file": read_repo_file,
    "safe_calculate": safe_calculate,
    "write_brief": write_brief,
    "generate_grafana_dashboard": generate_grafana_dashboard,
    "generate_grafana_alert_rule": generate_grafana_alert_rule,
    "generate_prompt_template": generate_prompt_template,
    "add_panel_to_dashboard": add_panel_to_dashboard,
    "lint_grafana_dashboard": lint_grafana_dashboard,
    "patch_grafana_dashboard": patch_grafana_dashboard,
    "check_prometheus_metrics": check_prometheus_metrics,
}

READ_ONLY_TOOLS = {
    "retrieve_context",
    "retrieve_repo_context",
    "read_repo_file",
    "safe_calculate",
}

ARTIFACT_TOOLS = {
    "write_brief",
    "generate_grafana_dashboard",
    "generate_grafana_alert_rule",
    "generate_prompt_template",
}

UPDATE_TOOLS = {"add_panel_to_dashboard"}

TOOL_DESCRIPTIONS = [
    {
        "name": "retrieve_context",
        "arguments": {"query": "string", "top_k": "int (optional, default 10)"},
        "purpose": "Retrieve relevant knowledge chunks for grounding.",
    },
    {
        "name": "retrieve_repo_context",
        "arguments": {"query": "string", "top_k": "int (optional, default 8)"},
        "purpose": "Retrieve relevant snippets from this repository (code, README, architecture) for grounded repo analysis.",
    },
    {
        "name": "read_repo_file",
        "arguments": {
            "path": "string (relative file path in repo)",
            "start_line": "int (optional, default 1)",
            "end_line": "int (optional, default 200)",
        },
        "purpose": "Read exact file content with line numbers for precise grounded answers.",
    },
    {
        "name": "safe_calculate",
        "arguments": {"expression": "string"},
        "purpose": "Do reliable arithmetic calculations.",
    },
    {
        "name": "check_prometheus_metrics",
        "arguments": {
            "metrics": "list[string] (optional)",
            "exprs": "list[string] (optional)",
            "base_url": "string (optional, default from PROMETHEUS_BASE_URL)",
        },
        "purpose": "Probe Prometheus metric availability and series counts for explicit metrics or parsed query expressions.",
    },
    {
        "name": "write_brief",
        "arguments": {"filename": "string", "content": "string"},
        "purpose": "Write a markdown brief to data/output for actionable delivery.",
    },
    {
        "name": "generate_grafana_dashboard",
        "arguments": {
            "title": "string",
            "datasource": "string (optional, default Prometheus)",
            "panels": 'list of {"title","type","expr","datasource_uid?"} (optional)',
            "filename": "string (optional)",
        },
        "purpose": "Generate reusable Grafana dashboard JSON code and save it to data/output.",
    },
    {
        "name": "generate_grafana_alert_rule",
        "arguments": {
            "title": "string",
            "expr": "string (PromQL or query)",
            "datasource_uid": "string (optional)",
            "for_duration": "string (optional, default 5m)",
            "severity": "string (optional, warning|critical)",
            "filename": "string (optional)",
        },
        "purpose": "Generate Grafana alerting provisioning JSON and save it to data/output.",
    },
    {
        "name": "generate_prompt_template",
        "arguments": {
            "name": "string",
            "purpose": "string",
            "instructions": "string",
            "inputs": "list[string] (optional)",
            "filename": "string (optional)",
        },
        "purpose": "Generate reusable prompt template JSON for DevOps workflows.",
    },
    {
        "name": "add_panel_to_dashboard",
        "arguments": {
            "filename": "string (existing dashboard JSON file name)",
            "panel_title": "string",
            "expr": "string",
            "panel_type": "string (optional, default timeseries)",
            "datasource": "string (optional, default Prometheus)",
        },
        "purpose": "Add a new panel to an existing generated Grafana dashboard JSON file.",
    },
    {
        "name": "lint_grafana_dashboard",
        "arguments": {
            "filename": "string (existing dashboard JSON file name)",
            "auto_fix": "bool (optional, default true)",
            "enforce_no_templating": "bool (optional, default true)",
            "required_datasource_uid": "string (optional, default default)",
            "profile": "string (optional, generic|kubernetes_pods)",
        },
        "purpose": "Lint and optionally auto-fix a generated dashboard JSON (schema, panel ids, grid, datasource uid, templating, PromQL quality).",
    },
    {
        "name": "patch_grafana_dashboard",
        "arguments": {
            "filename": "string (existing dashboard file in data/output)",
            "instructions": "string (optional)",
            "output_filename": "string (optional)",
            "profile": "string (optional, generic|kubernetes_pods)",
        },
        "purpose": "Patch/fix an existing dashboard JSON and produce a unified diff artifact for review.",
    },
]


def _chat(prompt: str, temperature: float = 0.0) -> str:
    try:
        response = client.chat.completions.create(
            model=LM_STUDIO_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        message = str(exc).lower()
        if "context length" in message or "number of tokens to keep" in message:
            tiny_prompt = (
                "Answer briefly in 3 bullet points using only available context. "
                "If missing context, say it is not covered."
            )
            try:
                response = client.chat.completions.create(
                    model=LM_STUDIO_CHAT_MODEL,
                    messages=[{"role": "user", "content": tiny_prompt}],
                    temperature=temperature,
                )
                return response.choices[0].message.content or ""
            except Exception:
                pass
        raise RuntimeError(
            "Failed to reach LM Studio chat endpoint. "
            "Ensure LM Studio server is running and the chat model is loaded."
        ) from exc


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("No JSON object found.")


def _looks_like_kubernetes_pod_dashboard_request(question: str) -> bool:
    lowered = question.lower()
    has_kubernetes = any(token in lowered for token in ("kubernetes", "k8s"))
    has_pod = "pod" in lowered
    has_dashboard_intent = any(
        token in lowered for token in ("grafana", "dashboard", "monitor")
    )
    return has_kubernetes and has_pod and has_dashboard_intent


def _ensure_kubernetes_pod_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _panel_text(panel: dict[str, Any]) -> str:
        return f"{str(panel.get('title', ''))} {str(panel.get('expr', ''))}".lower()

    def _has_panel(
        required_terms: tuple[str, ...], items: list[dict[str, Any]]
    ) -> bool:
        for item in items:
            text = _panel_text(item)
            if all(term in text for term in required_terms):
                return True
        return False

    required_panels: list[dict[str, Any]] = [
        {
            "title": "Pod CPU Usage (rate 5m)",
            "type": "timeseries",
            "expr": 'sum(rate(container_cpu_usage_seconds_total{namespace!="",pod!="",container!=""}[5m])) by (namespace, pod)',
            "legend": "{{namespace}}/{{pod}}",
            "unit": "cores",
            "_terms": ("cpu", "container_cpu_usage_seconds_total"),
        },
        {
            "title": "Pod Memory Working Set (MiB)",
            "type": "timeseries",
            "expr": 'sum(container_memory_working_set_bytes{namespace!="",pod!="",container!=""}) by (namespace, pod) / 1024 / 1024',
            "legend": "{{namespace}}/{{pod}}",
            "unit": "short",
            "_terms": ("memory", "container_memory_working_set_bytes"),
        },
        {
            "title": "Pod Restarts (last 1h)",
            "type": "stat",
            "expr": 'sum(increase(kube_pod_container_status_restarts_total{namespace!="",pod!="",container!=""}[1h])) by (namespace, pod)',
            "legend": "{{namespace}}/{{pod}}",
            "unit": "short",
            "_terms": ("restart", "kube_pod_container_status_restarts_total"),
        },
        {
            "title": "Running Pods by Namespace",
            "type": "barchart",
            "expr": 'sum(kube_pod_status_phase{phase="Running",namespace!="",pod!=""}) by (namespace)',
            "legend": "{{namespace}}",
            "unit": "short",
            "_terms": ("running", "kube_pod_status_phase"),
        },
        {
            "title": "Pending Pods by Namespace",
            "type": "barchart",
            "expr": 'sum(kube_pod_status_phase{phase="Pending",namespace!="",pod!=""}) by (namespace)',
            "legend": "{{namespace}}",
            "unit": "short",
            "_terms": ("pending", "kube_pod_status_phase"),
        },
        {
            "title": "Failed Pods by Namespace",
            "type": "barchart",
            "expr": 'sum(kube_pod_status_phase{phase="Failed",namespace!="",pod!=""}) by (namespace)',
            "legend": "{{namespace}}",
            "unit": "short",
            "_terms": ("failed", "kube_pod_status_phase"),
        },
    ]

    normalized: list[dict[str, Any]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        title = str(panel.get("title", "")).strip()
        expr = str(panel.get("expr", "")).strip()
        panel_type = (
            str(panel.get("type", "timeseries")).strip().lower() or "timeseries"
        )
        if panel_type not in {"timeseries", "stat", "barchart"}:
            panel_type = "timeseries"
        if not title or not expr:
            continue
        cleaned: dict[str, Any] = {
            "title": title[:120],
            "type": panel_type[:40],
            "expr": expr[:600],
        }
        for key, max_len in (("description", 180), ("legend", 120), ("unit", 40)):
            value = str(panel.get(key, "")).strip()
            if value:
                cleaned[key] = value[:max_len]
        grid_pos = panel.get("gridPos")
        if isinstance(grid_pos, dict):
            cleaned["gridPos"] = grid_pos
        normalized.append(cleaned)

    for required in required_panels:
        required_terms = required["_terms"]
        if _has_panel(required_terms=required_terms, items=normalized):
            continue
        normalized.append({k: v for k, v in required.items() if not k.startswith("_")})

    return normalized[:8]
