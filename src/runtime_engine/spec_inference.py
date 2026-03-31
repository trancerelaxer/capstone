"""Spec inference helpers for dashboard/alert/prompt generation."""

from runtime_engine.base import *  # noqa: F401,F403
from runtime_engine.base import (  # noqa: F401
    _chat,
    _ensure_kubernetes_pod_panels,
    _extract_json,
    _looks_like_kubernetes_pod_dashboard_request,
)


def _infer_dashboard_generation_spec(question: str) -> dict[str, Any] | None:
    prompt = f"""Create a Grafana dashboard generation spec.
Return JSON only:
{{
  "title": "string",
  "datasource": "Prometheus",
  "panels": [
    {{
      "title":"string",
      "type":"timeseries|barchart|stat",
      "expr":"promql",
      "description":"string (optional)",
      "legend":"string (optional)",
      "unit":"string (optional)"
    }}
  ]
}}

Rules:
- Prefer practical panels based on user intent.
- Keep 2-8 panels.
- Use Prometheus expressions.
- Output strict PromQL only (no SQL keywords like `where`).
- Use valid label matchers like `{{label="value"}}` or regex matcher `{{label=~".+"}}`.
- If request is about Kubernetes pod monitoring, include these panels:
  1) Pod CPU Usage (rate 5m) using `container_cpu_usage_seconds_total`
  2) Pod Memory Working Set using `container_memory_working_set_bytes`
  3) Pod Restarts in last 1h using `increase(kube_pod_container_status_restarts_total[1h])`
  4) Running Pods by namespace (`kube_pod_status_phase{{phase="Running"}}`)
  5) Pending Pods by namespace (`kube_pod_status_phase{{phase="Pending"}}`)
  6) Failed Pods by namespace (`kube_pod_status_phase{{phase="Failed"}}`)
- For pod/container metrics, include explicit non-empty filters where relevant:
  `namespace!=""`, `pod!=""`, `container!=""`.
- If unsure, still provide a valid generic dashboard.

User request:
{question[:MAX_QUESTION_CHARS]}
"""
    try:
        raw = _chat(prompt, temperature=0)
        parsed = _extract_json(raw)
        title = str(parsed.get("title", "")).strip() or "Generated Dashboard"
        datasource = str(parsed.get("datasource", "Prometheus")).strip() or "Prometheus"
        raw_panels = parsed.get("panels", [])
        panels: list[dict[str, Any]] = []
        if isinstance(raw_panels, list):
            for item in raw_panels[:8]:
                if not isinstance(item, dict):
                    continue
                p_title = str(item.get("title", "")).strip()
                p_type = (
                    str(item.get("type", "timeseries")).strip().lower() or "timeseries"
                )
                p_expr = str(item.get("expr", "")).strip()
                if p_type not in {"timeseries", "barchart", "stat"}:
                    p_type = "timeseries"
                if p_title and p_expr:
                    panel: dict[str, Any] = {
                        "title": p_title[:120],
                        "type": p_type[:40],
                        "expr": p_expr[:600],
                    }
                    for key, max_len in (
                        ("description", 180),
                        ("legend", 120),
                        ("unit", 40),
                    ):
                        value = str(item.get(key, "")).strip()
                        if value:
                            panel[key] = value[:max_len]
                    grid_pos = item.get("gridPos")
                    if isinstance(grid_pos, dict):
                        panel["gridPos"] = grid_pos
                    panels.append(panel)

        if _looks_like_kubernetes_pod_dashboard_request(question):
            panels = _ensure_kubernetes_pod_panels(panels)
        return {
            "title": title[:140],
            "datasource": datasource[:80],
            "panels": panels or None,
        }
    except Exception:
        return None


def _fallback_dashboard_generation_spec(question: str) -> dict[str, Any]:
    lowered = question.lower()
    if _looks_like_kubernetes_pod_dashboard_request(question):
        return {
            "title": "Kubernetes Pod Monitoring",
            "datasource": "Prometheus",
            "panels": _ensure_kubernetes_pod_panels([]),
        }

    panels: list[dict[str, Any]] = []
    if "latency" in lowered:
        panels.append(
            {
                "title": "API Latency P95",
                "type": "timeseries",
                "expr": "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
                "unit": "s",
            }
        )
    if "error" in lowered or "5xx" in lowered:
        panels.append(
            {
                "title": "API 5xx Error Rate",
                "type": "timeseries",
                "expr": 'sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))',
                "unit": "percentunit",
            }
        )
    if not panels:
        panels = [
            {
                "title": "Request Rate",
                "type": "timeseries",
                "expr": "sum(rate(http_requests_total[5m]))",
            },
            {
                "title": "Error Rate",
                "type": "timeseries",
                "expr": 'sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))',
                "unit": "percentunit",
            },
        ]
    return {
        "title": "Generated Dashboard",
        "datasource": "Prometheus",
        "panels": panels[:8],
    }


def _infer_alert_generation_spec(question: str) -> dict[str, str] | None:
    prompt = f"""Create a Grafana alert rule generation spec.
Return JSON only:
{{
  "title": "string",
  "expr": "promql expression",
  "severity": "warning|critical",
  "for_duration": "duration like 5m",
  "datasource_uid": "default"
}}

User request:
{question[:MAX_QUESTION_CHARS]}
"""
    try:
        raw = _chat(prompt, temperature=0)
        parsed = _extract_json(raw)
        title = str(parsed.get("title", "")).strip()
        expr = str(parsed.get("expr", "")).strip()
        severity = str(parsed.get("severity", "warning")).strip() or "warning"
        for_duration = str(parsed.get("for_duration", "5m")).strip() or "5m"
        datasource_uid = (
            str(parsed.get("datasource_uid", "default")).strip() or "default"
        )
        if not title or not expr:
            return None
        return {
            "title": title[:140],
            "expr": expr[:600],
            "severity": severity[:20],
            "for_duration": for_duration[:20],
            "datasource_uid": datasource_uid[:80],
        }
    except Exception:
        return None


def _infer_prompt_template_spec(question: str) -> dict[str, Any] | None:
    prompt = f"""Create a DevOps prompt-template generation spec.
Return JSON only:
{{
  "name": "string",
  "purpose": "string",
  "instructions": "string",
  "inputs": ["string", "string"]
}}

User request:
{question[:MAX_QUESTION_CHARS]}
"""
    try:
        raw = _chat(prompt, temperature=0)
        parsed = _extract_json(raw)
        name = str(parsed.get("name", "")).strip()
        purpose = str(parsed.get("purpose", "")).strip()
        instructions = str(parsed.get("instructions", "")).strip()
        if not name or not purpose or not instructions:
            return None
        raw_inputs = parsed.get("inputs", [])
        inputs: list[str] = []
        if isinstance(raw_inputs, list):
            for item in raw_inputs[:10]:
                value = str(item).strip()
                if value:
                    inputs.append(value[:80])
        return {
            "name": name[:140],
            "purpose": purpose[:180],
            "instructions": instructions[:1200],
            "inputs": inputs,
        }
    except Exception:
        return None
