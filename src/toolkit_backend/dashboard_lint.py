"""Dashboard lint/auto-fix implementation."""

from toolkit_backend.dashboard_lint_base import *  # noqa: F401,F403
from toolkit_backend.dashboard_lint_base import (  # noqa: F401
    _extract_panel_specs_from_dashboard,
    _panel_grid_errors,
    _rebuild_panels_from_specs,
    _resolve_output_dashboard_path,
)
from toolkit_backend.generation import _ensure_kubernetes_required_panels  # noqa: F401
from toolkit_backend.promql import (  # noqa: F401
    _adapt_expr_with_metric_availability,
    _apply_promql_quality_gate,
    _default_promql_for_panel_title,
    _sanitize_panel_type,
)


def lint_grafana_dashboard(
    filename: str,
    auto_fix: bool = True,
    enforce_no_templating: bool = True,
    required_datasource_uid: str = "default",
    profile: str = "generic",
    check_metric_availability: bool = True,
    prometheus_base_url: str | None = None,
) -> dict[str, Any]:
    path = _resolve_output_dashboard_path(filename)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Dashboard file not found: {Path(filename).name}")

    issues: list[str] = []
    warnings: list[str] = []
    fixed_count = 0
    fixed = False
    retriable_failure = False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "dashboard_file": path.name,
            "issues": [f"Invalid JSON: {exc}"],
            "warnings": [],
            "fixed": False,
            "fixed_count": 0,
            "need_retry_generation": True,
            "profile": profile,
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "dashboard_file": path.name,
            "issues": ["Top-level JSON must be an object."],
            "warnings": [],
            "fixed": False,
            "fixed_count": 0,
            "need_retry_generation": True,
            "profile": profile,
        }

    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        issues.append('Missing or invalid "dashboard" object.')
        if auto_fix:
            dashboard = {
                "uid": None,
                "title": "Generated Dashboard",
                "schemaVersion": 39,
                "version": 1,
                "panels": [],
                "templating": {"list": []},
                "annotations": {"list": []},
            }
            payload["dashboard"] = dashboard
            fixed = True
            fixed_count += 1
        else:
            retriable_failure = True
            return {
                "ok": False,
                "dashboard_file": path.name,
                "issues": issues,
                "warnings": warnings,
                "fixed": False,
                "fixed_count": 0,
                "need_retry_generation": True,
                "profile": profile,
            }

    schema_version = dashboard.get("schemaVersion")
    try:
        schema_version_int = int(schema_version)
    except Exception:
        schema_version_int = 0
    if schema_version_int < 39:
        issues.append("schemaVersion must be >= 39.")
        if auto_fix:
            dashboard["schemaVersion"] = 39
            fixed = True
            fixed_count += 1
        else:
            retriable_failure = True

    templating = dashboard.get("templating")
    templating_list = templating.get("list") if isinstance(templating, dict) else None
    if enforce_no_templating:
        if not isinstance(templating_list, list):
            issues.append("templating.list must exist and be a list.")
            if auto_fix:
                dashboard["templating"] = {"list": []}
                fixed = True
                fixed_count += 1
            else:
                retriable_failure = True
        elif len(templating_list) > 0:
            issues.append(
                "templating variables are not allowed for this assistant output."
            )
            if auto_fix:
                dashboard["templating"] = {"list": []}
                fixed = True
                fixed_count += 1
            else:
                retriable_failure = True

    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        issues.append("dashboard.panels must be a list.")
        if auto_fix:
            panels = []
            dashboard["panels"] = panels
            fixed = True
            fixed_count += 1
        else:
            retriable_failure = True

    if not isinstance(panels, list):
        panels = []

    # Enforce panel ids uniqueness and ordering.
    seen_ids: set[int] = set()
    panel_id_errors = False
    for panel in panels:
        if not isinstance(panel, dict):
            panel_id_errors = True
            break
        try:
            panel_id = int(panel.get("id", 0))
        except Exception:
            panel_id = 0
        if panel_id <= 0 or panel_id in seen_ids:
            panel_id_errors = True
            break
        seen_ids.add(panel_id)
    if panel_id_errors:
        issues.append("Panel IDs must be unique positive integers.")
        if auto_fix:
            for idx, panel in enumerate(panels, start=1):
                if isinstance(panel, dict):
                    panel["id"] = idx
            fixed = True
            fixed_count += 1
        else:
            retriable_failure = True

    datasource_uid = str(required_datasource_uid or "default").strip() or "default"
    datasource_type_default = "Prometheus"

    metric_probe: dict[str, Any] | None = None
    if check_metric_availability:
        exprs_for_probe: list[str] = []
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            targets = panel.get("targets", [])
            if not isinstance(targets, list):
                continue
            for target in targets:
                if not isinstance(target, dict):
                    continue
                expr = str(target.get("expr", "")).strip()
                if expr:
                    exprs_for_probe.append(expr)
        if exprs_for_probe:
            try:
                metric_probe = check_prometheus_metrics(
                    exprs=exprs_for_probe, base_url=prometheus_base_url
                )
                if metric_probe.get("errors"):
                    warnings.append(
                        "Metric probe completed with errors; Prometheus may be unavailable."
                    )
            except Exception as exc:
                metric_probe = {
                    "base_url": str(prometheus_base_url or PROMETHEUS_BASE_URL),
                    "metric_count": 0,
                    "available_count": 0,
                    "availability": {},
                    "errors": [str(exc)],
                }
                warnings.append(f"Metric probe failed: {exc}")

    for idx, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            continue
        ds = panel.get("datasource")
        if not isinstance(ds, dict):
            issues.append(f"Panel #{idx}: missing datasource object.")
            if auto_fix:
                panel["datasource"] = {
                    "type": datasource_type_default,
                    "uid": datasource_uid,
                }
                fixed = True
                fixed_count += 1
                ds = panel["datasource"]
            else:
                retriable_failure = True
                continue
        current_uid = str(ds.get("uid", "")).strip()
        if current_uid != datasource_uid:
            issues.append(f'Panel #{idx}: datasource uid must be "{datasource_uid}".')
            if auto_fix:
                ds["uid"] = datasource_uid
                if not str(ds.get("type", "")).strip():
                    ds["type"] = datasource_type_default
                fixed = True
                fixed_count += 1
            else:
                retriable_failure = True

        panel_title = str(panel.get("title", f"Panel {idx}"))
        targets = panel.get("targets")
        if not isinstance(targets, list) or len(targets) == 0:
            issues.append(f"Panel #{idx}: missing targets.")
            if auto_fix:
                default_expr = _default_promql_for_panel_title(panel_title) or "up"
                panel["targets"] = [
                    {
                        "refId": "A",
                        "expr": default_expr,
                        "editorMode": "code",
                        "legendFormat": "",
                        "instant": _sanitize_panel_type(panel.get("type", "timeseries"))
                        in {"stat", "barchart"},
                        "range": _sanitize_panel_type(panel.get("type", "timeseries"))
                        == "timeseries",
                    }
                ]
                fixed = True
                fixed_count += 1
                targets = panel["targets"]
            else:
                retriable_failure = True
                continue

        if not isinstance(targets, list):
            continue
        for t_idx, target in enumerate(targets, start=1):
            if not isinstance(target, dict):
                issues.append(
                    f"Panel #{idx} target #{t_idx}: target must be an object."
                )
                continue
            expr = str(target.get("expr", "")).strip()
            gate = _apply_promql_quality_gate(
                panel_title=panel_title, expr=expr, variable_filters=None
            )
            for gate_issue in gate.get("issues", []):
                warnings.append(f"Panel #{idx} target #{t_idx}: {gate_issue}")
            if gate.get("changed") and auto_fix:
                target["expr"] = gate.get("expr", expr)
                fixed = True
                fixed_count += 1
            elif gate.get("changed"):
                issues.append(
                    f"Panel #{idx} target #{t_idx}: PromQL quality gate failed."
                )
                retriable_failure = True
            if gate.get("weak"):
                issues.append(
                    f"Panel #{idx} target #{t_idx}: weak expression remains after quality gate."
                )
                retriable_failure = True

            expr_after_gate = str(target.get("expr", "")).strip()
            adapted_expr, availability_warnings = _adapt_expr_with_metric_availability(
                expr=expr_after_gate,
                metric_probe=metric_probe,
            )
            for availability_warning in availability_warnings:
                warnings.append(f"Panel #{idx} target #{t_idx}: {availability_warning}")
            if adapted_expr != expr_after_gate and auto_fix:
                target["expr"] = adapted_expr
                fixed = True
                fixed_count += 1

    grid_errors = _panel_grid_errors(panels)
    if grid_errors:
        issues.extend(grid_errors)
        if auto_fix:
            panel_specs = _extract_panel_specs_from_dashboard(panels)
            if panel_specs:
                dashboard["panels"] = _rebuild_panels_from_specs(
                    panel_specs=panel_specs,
                    datasource=datasource_type_default,
                    datasource_uid=datasource_uid,
                )
                panels = dashboard["panels"]
                fixed = True
                fixed_count += 1
            else:
                retriable_failure = True
        else:
            retriable_failure = True

    missing_required_panels: list[str] = []
    if str(profile).strip().lower() == "kubernetes_pods":
        panel_specs = _extract_panel_specs_from_dashboard(panels)
        ensured_specs, missing_required_panels = _ensure_kubernetes_required_panels(
            panel_specs
        )
        if missing_required_panels:
            issues.append(
                "Missing required Kubernetes pod panels: "
                + ", ".join(missing_required_panels)
            )
            if auto_fix:
                dashboard["panels"] = _rebuild_panels_from_specs(
                    panel_specs=ensured_specs,
                    datasource=datasource_type_default,
                    datasource_uid=datasource_uid,
                )
                panels = dashboard["panels"]
                fixed = True
                fixed_count += 1
            else:
                retriable_failure = True

    if fixed:
        path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8"
        )

    post_issues: list[str] = []
    if str(profile).strip().lower() == "kubernetes_pods":
        post_specs = _extract_panel_specs_from_dashboard(dashboard.get("panels", []))
        _, post_missing = _ensure_kubernetes_required_panels(post_specs)
        if post_missing:
            post_issues.append(
                "Still missing required Kubernetes pod panels after fixes: "
                + ", ".join(post_missing)
            )
    post_grid_errors = _panel_grid_errors(
        dashboard.get("panels", []) if isinstance(dashboard.get("panels"), list) else []
    )
    post_issues.extend(post_grid_errors)

    if enforce_no_templating:
        post_templating = dashboard.get("templating")
        post_templating_list = (
            post_templating.get("list") if isinstance(post_templating, dict) else None
        )
        if not isinstance(post_templating_list, list) or len(post_templating_list) > 0:
            post_issues.append("templating.list is not empty.")

    final_ok = len(post_issues) == 0 and not retriable_failure
    need_retry_generation = (not final_ok) and (
        retriable_failure or len(post_issues) > 0
    )

    return {
        "ok": final_ok,
        "dashboard_file": path.name,
        "issues": issues + post_issues,
        "warnings": warnings,
        "fixed": fixed,
        "fixed_count": fixed_count,
        "profile": str(profile).strip().lower() or "generic",
        "need_retry_generation": need_retry_generation,
        "missing_required_panels": missing_required_panels,
        "panel_count": (
            len(dashboard.get("panels", []))
            if isinstance(dashboard.get("panels"), list)
            else 0
        ),
        "metric_probe": metric_probe or {},
    }


def _infer_lint_profile(instructions: str, fallback_profile: str = "generic") -> str:
    lowered = str(instructions or "").lower()
    if any(token in lowered for token in ("kubernetes", "k8s")) and "pod" in lowered:
        return "kubernetes_pods"
    return str(fallback_profile or "generic").strip().lower() or "generic"


def _extract_required_uid(instructions: str, default_uid: str = "default") -> str:
    text = str(instructions or "")
    explicit = re.search(
        r'datasource\s+uid\s*[:=]?\s*"([^"]+)"', text, flags=re.IGNORECASE
    )
    if explicit:
        candidate = explicit.group(1).strip()
        if candidate:
            return candidate
    quoted_uid = re.search(r'uid\s*[:=]?\s*"([^"]+)"', text, flags=re.IGNORECASE)
    if quoted_uid:
        candidate = quoted_uid.group(1).strip()
        if candidate:
            return candidate
    return str(default_uid or "default").strip() or "default"
