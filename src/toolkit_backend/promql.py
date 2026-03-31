"""PromQL normalization, quality gates, and metric probing logic."""

from toolkit_backend.retrieval import *  # noqa: F401,F403
from toolkit_backend.retrieval import _normalize_promql  # noqa: F401
from toolkit_backend.base import (
    _extract_metric_names_from_expr,
    _http_get_json,
    _replace_metric_in_expr,
)  # noqa: F401


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return default


def _sanitize_panel_type(panel_type: Any) -> str:
    candidate = str(panel_type or "timeseries").strip().lower()
    return candidate if candidate in ALLOWED_PANEL_TYPES else "timeseries"


def _ref_id_for_index(index: int) -> str:
    if index <= 26:
        return chr(64 + index)
    return f"A{index - 26}"


def _default_grid_pos(panel_index: int, panel_count: int) -> dict[str, int]:
    if panel_count <= 4:
        return {
            "h": 8,
            "w": 12,
            "x": ((panel_index - 1) % 2) * 12,
            "y": ((panel_index - 1) // 2) * 8,
        }

    if panel_index <= 2:
        return {"h": 8, "w": 12, "x": ((panel_index - 1) % 2) * 12, "y": 0}

    remaining = max(1, panel_count - 2)
    cols = min(4, remaining)
    width = 24 // cols
    rel_index = panel_index - 3
    return {
        "h": 6,
        "w": width,
        "x": (rel_index % cols) * width,
        "y": 8 + (rel_index // cols) * 6,
    }


def _coerce_grid_pos(raw_grid: Any, fallback: dict[str, int]) -> dict[str, int]:
    if not isinstance(raw_grid, dict):
        return fallback
    try:
        h = int(raw_grid.get("h", fallback["h"]))
        w = int(raw_grid.get("w", fallback["w"]))
        x = int(raw_grid.get("x", fallback["x"]))
        y = int(raw_grid.get("y", fallback["y"]))
    except Exception:
        return fallback

    if h <= 0 or h > 24:
        return fallback
    if w <= 0 or w > 24:
        return fallback
    if x < 0 or y < 0:
        return fallback
    if x + w > 24:
        return fallback
    return {"h": h, "w": w, "x": x, "y": y}


def _infer_unit(panel_title: str, expr: str) -> str | None:
    lowered_title = panel_title.lower()
    lowered_expr = expr.lower()
    compact_expr = re.sub(r"\s+", "", lowered_expr)

    if "latency" in lowered_title or "duration" in lowered_title:
        return "s"
    if (
        "error rate" in lowered_title
        or "success rate" in lowered_title
        or "percentage" in lowered_title
    ):
        return "percentunit"
    if "cpu" in lowered_title or "cpu_usage_seconds_total" in lowered_expr:
        return "cores"
    if "memory" in lowered_title or "memory_" in lowered_expr:
        if "/1024/1024" in compact_expr:
            return "short"
        return "bytes"
    return None


def _default_field_config(
    panel_type: str, panel_title: str, expr: str, spec: dict[str, Any]
) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "mappings": [],
        "thresholds": {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}, {"color": "red", "value": 80}],
        },
    }

    requested_unit = str(spec.get("unit", "")).strip()
    inferred_unit = _infer_unit(panel_title=panel_title, expr=expr)
    unit = requested_unit or inferred_unit
    if unit:
        defaults["unit"] = unit

    for numeric_key in ("min", "max"):
        if numeric_key not in spec:
            continue
        try:
            defaults[numeric_key] = float(spec[numeric_key])
        except Exception:
            continue

    if "decimals" in spec:
        try:
            defaults["decimals"] = int(spec["decimals"])
        except Exception:
            pass

    if panel_type == "timeseries":
        defaults["custom"] = {
            "drawStyle": "line",
            "lineInterpolation": "linear",
            "barAlignment": 0,
            "lineWidth": 2,
            "fillOpacity": 10,
            "gradientMode": "none",
            "spanNulls": False,
            "showPoints": "auto",
            "pointSize": 4,
            "stacking": {"mode": "none", "group": "A"},
            "axisPlacement": "auto",
            "axisLabel": "",
            "axisCenteredZero": False,
            "axisColorMode": "text",
            "axisBorderShow": False,
            "scaleDistribution": {"type": "linear"},
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "thresholdsStyle": {"mode": "off"},
        }

    return {"defaults": defaults, "overrides": []}


def _default_panel_options(panel_type: str) -> dict[str, Any]:
    if panel_type == "stat":
        return {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "wideLayout": True,
        }

    if panel_type == "barchart":
        return {
            "orientation": "auto",
            "xTickLabelRotation": 0,
            "xTickLabelSpacing": 0,
            "showValue": "auto",
            "stacking": "none",
            "groupWidth": 0.7,
            "barWidth": 0.97,
            "barRadius": 0,
            "fullHighlight": False,
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
                "calcs": [],
            },
            "tooltip": {"mode": "single", "sort": "none"},
        }

    return {
        "legend": {
            "showLegend": True,
            "displayMode": "list",
            "placement": "bottom",
            "calcs": [],
        },
        "tooltip": {"mode": "single", "sort": "none"},
    }


def _default_promql_for_panel_title(panel_title: str) -> str | None:
    lowered_title = panel_title.lower()
    if "cpu" in lowered_title:
        return 'sum(rate(container_cpu_usage_seconds_total{namespace!="",pod!="",container!=""}[5m])) by (namespace, pod)'
    if "memory" in lowered_title:
        return 'sum(container_memory_working_set_bytes{namespace!="",pod!="",container!=""}) by (namespace, pod) / 1024 / 1024'
    if "restart" in lowered_title:
        return 'sum(increase(kube_pod_container_status_restarts_total{namespace!="",pod!="",container!=""}[1h])) by (namespace, pod)'
    if "running" in lowered_title:
        return 'sum(kube_pod_status_phase{phase="Running",namespace!="",pod!=""}) by (namespace)'
    if "pending" in lowered_title:
        return 'sum(kube_pod_status_phase{phase="Pending",namespace!="",pod!=""}) by (namespace)'
    if "failed" in lowered_title:
        return 'sum(kube_pod_status_phase{phase="Failed",namespace!="",pod!=""}) by (namespace)'
    return None


def check_prometheus_metrics(
    metrics: list[str] | None = None,
    exprs: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = PROMETHEUS_TIMEOUT_SEC,
) -> dict[str, Any]:
    resolved_base_url = str(base_url or PROMETHEUS_BASE_URL).strip().rstrip("/")
    if not resolved_base_url:
        raise ValueError("Prometheus base URL is required.")

    candidate_metrics: list[str] = []
    if isinstance(metrics, list):
        for metric in metrics:
            name = str(metric).strip()
            if name and name not in candidate_metrics:
                candidate_metrics.append(name)

    if isinstance(exprs, list):
        for expr in exprs:
            for metric_name in _extract_metric_names_from_expr(str(expr)):
                if metric_name not in candidate_metrics:
                    candidate_metrics.append(metric_name)

    if not candidate_metrics:
        return {
            "base_url": resolved_base_url,
            "metric_count": 0,
            "available_count": 0,
            "availability": {},
            "errors": [],
        }

    availability: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    probe_blocked_error = ""

    for metric_name in candidate_metrics:
        if probe_blocked_error:
            availability[metric_name] = {
                "exists": False,
                "series_count": 0.0,
                "status": "error",
                "error": probe_blocked_error,
            }
            continue
        query = f"count({metric_name})"
        url = f"{resolved_base_url}/api/v1/query?{urlencode({'query': query})}"
        try:
            response = _http_get_json(url, timeout_seconds=timeout_seconds)
            status = str(response.get("status", "")).strip().lower()
            data = response.get("data", {})
            result = data.get("result", []) if isinstance(data, dict) else []
            value = 0.0
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict):
                    sample_value = first.get("value", [])
                    if isinstance(sample_value, list) and len(sample_value) >= 2:
                        try:
                            value = float(sample_value[1])
                        except Exception:
                            value = 0.0
            exists = status == "success" and value > 0
            availability[metric_name] = {
                "exists": exists,
                "series_count": value,
                "status": status or "unknown",
            }
        except Exception as exc:
            probe_blocked_error = str(exc)
            availability[metric_name] = {
                "exists": False,
                "series_count": 0.0,
                "status": "error",
                "error": probe_blocked_error,
            }
            errors.append(f"{metric_name}: {probe_blocked_error}")

    available_count = sum(
        1 for item in availability.values() if bool(item.get("exists"))
    )
    return {
        "base_url": resolved_base_url,
        "metric_count": len(candidate_metrics),
        "available_count": available_count,
        "availability": availability,
        "errors": errors,
    }


def _adapt_expr_with_metric_availability(
    expr: str,
    metric_probe: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    availability = (
        metric_probe.get("availability", {}) if isinstance(metric_probe, dict) else {}
    )
    if not isinstance(availability, dict) or not availability:
        return expr, []

    warnings: list[str] = []
    adapted = expr
    metric_names = _extract_metric_names_from_expr(expr)
    for metric_name in metric_names:
        metric_state = availability.get(metric_name, {})
        if not isinstance(metric_state, dict):
            continue
        if bool(metric_state.get("exists")):
            continue
        fallbacks = PROMQL_METRIC_FALLBACKS.get(metric_name, [])
        replacement = ""
        for candidate in fallbacks:
            candidate_state = availability.get(candidate, {})
            if isinstance(candidate_state, dict) and bool(
                candidate_state.get("exists")
            ):
                replacement = candidate
                break
        if replacement:
            adapted = _replace_metric_in_expr(adapted, metric_name, replacement)
            warnings.append(
                f"Metric `{metric_name}` unavailable; used fallback `{replacement}`."
            )
        else:
            warnings.append(
                f"Metric `{metric_name}` unavailable and no fallback found."
            )
    return adapted, warnings


def _is_weak_promql(expr: str) -> bool:
    lowered = re.sub(r"\s+", "", expr.lower())
    return lowered in {"up", "sum(up)", "avg(up)", "max(up)", "min(up)"}


def _apply_promql_quality_gate(
    panel_title: str, expr: str, variable_filters: dict[str, str] | None = None
) -> dict[str, Any]:
    issues: list[str] = []
    original_expr = str(expr or "").strip()
    candidate = _normalize_promql(original_expr, variable_filters=variable_filters)
    lowered_title = panel_title.lower()
    lowered_expr = candidate.lower()

    if _is_weak_promql(candidate):
        issues.append("Weak PromQL expression (`up`) detected.")
        fallback = _default_promql_for_panel_title(panel_title)
        if fallback:
            candidate = fallback
            lowered_expr = candidate.lower()

    if "restart" in lowered_title and "container_startup_time_seconds" in lowered_expr:
        issues.append(
            "Wrong restart metric detected (`container_startup_time_seconds`)."
        )
        fallback = _default_promql_for_panel_title(panel_title)
        if fallback:
            candidate = fallback
            lowered_expr = candidate.lower()

    if (
        "restart" in lowered_title
        and "kube_pod_container_status_restarts_total" in lowered_expr
    ):
        if "increase(" not in lowered_expr and "rate(" not in lowered_expr:
            issues.append("Restart query missing `increase()` or `rate()` window.")
            fallback = _default_promql_for_panel_title(panel_title)
            if fallback:
                candidate = fallback
                lowered_expr = candidate.lower()

    if "cpu" in lowered_title and "container_cpu_usage_seconds_total" in lowered_expr:
        if (
            "rate(" not in lowered_expr
            and "irate(" not in lowered_expr
            and "increase(" not in lowered_expr
        ):
            issues.append("CPU usage query missing `rate()`-style function.")
            fallback = _default_promql_for_panel_title(panel_title)
            if fallback:
                candidate = fallback
                lowered_expr = candidate.lower()

    if (
        "memory" in lowered_title
        and "container_memory_working_set_bytes" not in lowered_expr
    ):
        fallback = _default_promql_for_panel_title(panel_title)
        if fallback:
            issues.append(
                "Memory query does not use `container_memory_working_set_bytes`."
            )
            candidate = fallback
            lowered_expr = candidate.lower()

    for phase in ("running", "pending", "failed"):
        if phase in lowered_title:
            phase_metric_ok = (
                "kube_pod_status_phase" in lowered_expr
                and f'phase="{phase.capitalize()}"'.lower() in lowered_expr
            )
            if not phase_metric_ok:
                issues.append(
                    f"{phase.capitalize()} panel query should use `kube_pod_status_phase` with matching phase."
                )
                fallback = _default_promql_for_panel_title(panel_title)
                if fallback:
                    candidate = fallback
                    lowered_expr = candidate.lower()
            break

    candidate = _normalize_promql(candidate, variable_filters=variable_filters)
    changed = candidate != original_expr
    return {
        "expr": candidate,
        "changed": changed,
        "issues": issues,
        "weak": _is_weak_promql(candidate),
    }
