"""Grafana dashboard/alert/prompt generation implementations."""

from toolkit_backend.promql import *  # noqa: F401,F403
from toolkit_backend.retrieval import _slugify  # noqa: F401
from toolkit_backend.base import _resolve_output_file_path  # noqa: F401
from toolkit_backend.promql import (  # noqa: F401
    _apply_promql_quality_gate,
    _coerce_bool,
    _coerce_grid_pos,
    _default_field_config,
    _default_grid_pos,
    _default_panel_options,
    _ref_id_for_index,
    _sanitize_panel_type,
)


def _kubernetes_required_panels() -> list[dict[str, Any]]:
    return [
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


def _panel_text_signature(panel: dict[str, Any]) -> str:
    return f"{str(panel.get('title', ''))} {str(panel.get('expr', ''))}".lower()


def _ensure_kubernetes_required_panels(
    panels: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    normalized: list[dict[str, Any]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        title = str(panel.get("title", "")).strip()
        expr = str(panel.get("expr", "")).strip()
        if not title or not expr:
            continue
        panel_type = _sanitize_panel_type(panel.get("type", "timeseries"))
        cleaned: dict[str, Any] = {
            "title": title[:120],
            "type": panel_type,
            "expr": expr[:700],
        }
        for key, max_len in (("description", 180), ("legend", 120), ("unit", 40)):
            value = str(panel.get(key, "")).strip()
            if value:
                cleaned[key] = value[:max_len]
        normalized.append(cleaned)

    missing_titles: list[str] = []
    for required in _kubernetes_required_panels():
        terms = required["_terms"]
        exists = any(
            all(term in _panel_text_signature(panel) for term in terms)
            for panel in normalized
        )
        if exists:
            continue
        missing_titles.append(str(required["title"]))
        normalized.append({k: v for k, v in required.items() if not k.startswith("_")})

    return normalized[:12], missing_titles


def _build_target(
    panel_type: str, ref_id: str, expr: str, spec: dict[str, Any]
) -> dict[str, Any]:
    instant_default = panel_type in {"stat", "barchart"}
    instant = _coerce_bool(spec.get("instant"), default=instant_default)
    range_mode = _coerce_bool(spec.get("range"), default=not instant)

    legend_format = str(spec.get("legend", "")).strip()
    target: dict[str, Any] = {
        "refId": ref_id,
        "expr": expr,
        "editorMode": "code",
        "legendFormat": legend_format,
        "instant": instant,
        "range": range_mode,
    }
    interval = str(spec.get("interval", "")).strip()
    if interval:
        target["interval"] = interval
    return target


def _build_dashboard_panel(
    panel_index: int,
    panel_count: int,
    spec: dict[str, Any],
    datasource: str,
    variable_filters: dict[str, str] | None,
) -> dict[str, Any]:
    panel_title = (
        str(spec.get("title", f"Panel {panel_index}")).strip() or f"Panel {panel_index}"
    )
    panel_type = _sanitize_panel_type(spec.get("type", "timeseries"))
    expr_raw = str(spec.get("expr", "up"))
    gate_result = _apply_promql_quality_gate(
        panel_title=panel_title, expr=expr_raw, variable_filters=variable_filters
    )
    expr = str(gate_result.get("expr", "up")).strip() or "up"

    datasource_uid = str(spec.get("datasource_uid", "default")).strip() or "default"
    fallback_grid = _default_grid_pos(panel_index=panel_index, panel_count=panel_count)
    grid_pos = _coerce_grid_pos(spec.get("gridPos"), fallback=fallback_grid)

    panel: dict[str, Any] = {
        "id": panel_index,
        "title": panel_title,
        "type": panel_type,
        "datasource": {"type": datasource, "uid": datasource_uid},
        "targets": [
            _build_target(
                panel_type=panel_type,
                ref_id=_ref_id_for_index(panel_index),
                expr=expr,
                spec=spec,
            )
        ],
        "gridPos": grid_pos,
        "fieldConfig": _default_field_config(
            panel_type=panel_type, panel_title=panel_title, expr=expr, spec=spec
        ),
        "options": _default_panel_options(panel_type=panel_type),
        "transparent": False,
    }

    description = str(spec.get("description", "")).strip()
    if description:
        panel["description"] = description
    return panel


def generate_grafana_dashboard(
    title: str,
    datasource: str = "Prometheus",
    panels: list[dict[str, Any]] | None = None,
    filename: str | None = None,
    templating: list[dict[str, Any]] | None = None,
    variable_filters: dict[str, str] | None = None,
) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    effective_variable_filters = (
        variable_filters
        if isinstance(variable_filters, dict) and variable_filters
        else None
    )

    panel_specs = (
        panels
        if isinstance(panels, list) and panels
        else [
            {
                "title": "CPU Usage",
                "type": "timeseries",
                "expr": 'sum(rate(node_cpu_seconds_total{mode!="idle"}[5m]))',
            },
            {
                "title": "Memory Usage",
                "type": "timeseries",
                "expr": "node_memory_MemAvailable_bytes",
            },
        ]
    )
    panel_specs = [spec for spec in panel_specs if isinstance(spec, dict)] or [
        {
            "title": "CPU Usage",
            "type": "timeseries",
            "expr": 'sum(rate(node_cpu_seconds_total{mode!="idle"}[5m]))',
        },
        {
            "title": "Memory Usage",
            "type": "timeseries",
            "expr": "node_memory_MemAvailable_bytes",
        },
    ]
    panel_specs = panel_specs[:12]

    dashboard_panels: list[dict[str, Any]] = []
    for idx, spec in enumerate(panel_specs, start=1):
        dashboard_panels.append(
            _build_dashboard_panel(
                panel_index=idx,
                panel_count=len(panel_specs),
                spec=spec,
                datasource=datasource,
                variable_filters=effective_variable_filters,
            )
        )

    templating_list = templating if isinstance(templating, list) else []

    dashboard = {
        "uid": None,
        "title": title,
        "tags": ["generated", "agentic", "devops"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 0,
        "style": "dark",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h"],
            "time_options": ["5m", "15m", "1h", "6h", "12h", "24h", "2d", "7d", "30d"],
        },
        "fiscalYearStartMonth": 0,
        "weekStart": "",
        "liveNow": False,
        "links": [],
        "panels": dashboard_panels,
        "templating": {"list": templating_list},
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "datasource", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
    }

    payload = {
        "dashboard": dashboard,
        "folderUid": None,
        "message": "generated by agent",
        "overwrite": False,
    }
    safe_name = Path(filename).name if filename else f"{_slugify(title)}.dashboard.json"
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}.json"
    path = OUTPUT_DIR / safe_name
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2))
    return {
        "saved_to": str(path),
        "dashboard_title": title,
        "panel_count": len(dashboard_panels),
        "json": payload,
    }


def generate_grafana_alert_rule(
    title: str,
    expr: str,
    datasource_uid: str = "default",
    for_duration: str = "5m",
    severity: str = "warning",
    filename: str | None = None,
) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "apiVersion": 1,
        "groups": [
            {
                "name": f"{_slugify(title)}-group",
                "folder": "General",
                "interval": "1m",
                "rules": [
                    {
                        "uid": "",
                        "title": title,
                        "condition": "A",
                        "data": [
                            {
                                "refId": "A",
                                "datasourceUid": datasource_uid,
                                "model": {
                                    "expr": expr,
                                    "intervalMs": 1000,
                                    "maxDataPoints": 43200,
                                    "refId": "A",
                                },
                            }
                        ],
                        "for": for_duration,
                        "labels": {"severity": severity},
                        "annotations": {"summary": title},
                        "noDataState": "NoData",
                        "execErrState": "Error",
                    }
                ],
            }
        ],
    }
    safe_name = Path(filename).name if filename else f"{_slugify(title)}.alert.json"
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}.json"
    path = OUTPUT_DIR / safe_name
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2))
    return {"saved_to": str(path), "alert_title": title, "json": payload}


def generate_prompt_template(
    name: str,
    purpose: str,
    instructions: str,
    inputs: list[str] | None = None,
    filename: str | None = None,
) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "purpose": purpose,
        "inputs": inputs or [],
        "template": {
            "system": "You are a DevOps assistant focused on Grafana and observability best practices.",
            "instructions": instructions,
        },
    }
    safe_name = Path(filename).name if filename else f"{_slugify(name)}.prompt.json"
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}.json"
    path = OUTPUT_DIR / safe_name
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2))
    return {"saved_to": str(path), "prompt_name": name, "json": payload}


def add_panel_to_dashboard(
    filename: str,
    panel_title: str,
    expr: str,
    panel_type: str = "timeseries",
    datasource: str = "Prometheus",
) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    path = _resolve_output_file_path(
        safe_name, migrate_legacy_to_canonical=True
    )
    if not path.exists():
        raise FileNotFoundError(f"Dashboard file not found: {safe_name}")

    payload = json.loads(path.read_text())
    dashboard = payload.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValueError("Invalid dashboard JSON structure.")

    panels = dashboard.get("panels", [])
    if not isinstance(panels, list):
        panels = []

    next_id = (
        max([int(p.get("id", 0)) for p in panels if isinstance(p, dict)] + [0]) + 1
    )
    next_y = max(
        [
            int(p.get("gridPos", {}).get("y", 0))
            + int(p.get("gridPos", {}).get("h", 0))
            for p in panels
            if isinstance(p, dict)
        ]
        + [0]
    )

    new_spec = {
        "title": panel_title,
        "type": panel_type,
        "expr": expr,
        "datasource_uid": "default",
        "gridPos": {
            "h": 8 if _sanitize_panel_type(panel_type) == "timeseries" else 6,
            "w": 24,
            "x": 0,
            "y": next_y,
        },
    }
    new_panel = _build_dashboard_panel(
        panel_index=next_id,
        panel_count=max(1, len(panels) + 1),
        spec=new_spec,
        datasource=datasource,
        variable_filters=None,
    )
    panels.append(new_panel)
    dashboard["panels"] = panels
    payload["dashboard"] = dashboard
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2))
    return {
        "saved_to": str(path),
        "dashboard_file": safe_name,
        "panel_title": panel_title,
    }
