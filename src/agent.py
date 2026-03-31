"""Unified agent entrypoint for runtime and toolkit exports."""

from typing import Any

from toolkit_backend.dashboard_lint import (
    lint_grafana_dashboard as _lint_grafana_dashboard,
)
from toolkit_backend.dashboard_patch import (
    patch_grafana_dashboard as _patch_grafana_dashboard,
)
from toolkit_backend.generation import (
    add_panel_to_dashboard as _add_panel_to_dashboard,
    generate_grafana_alert_rule as _generate_grafana_alert_rule,
    generate_grafana_dashboard as _generate_grafana_dashboard,
    generate_prompt_template as _generate_prompt_template,
)
from toolkit_backend.promql import (
    check_prometheus_metrics as _check_prometheus_metrics,
)
from toolkit_backend.retrieval import (
    read_repo_file as _read_repo_file,
    retrieve_context as _retrieve_context,
    retrieve_repo_context as _retrieve_repo_context,
    safe_calculate as _safe_calculate,
    write_brief as _write_brief,
)

ARTIFACT_TOOL_NAMES = (
    "generate_grafana_dashboard",
    "generate_grafana_alert_rule",
    "generate_prompt_template",
)

REFLECTION_SCORE_FIELDS = (
    "groundedness_score",
    "completeness_score",
    "actionability_score",
)


def retrieve_context(query: str, top_k: int = 10) -> dict:
    return _retrieve_context(query=query, top_k=top_k)


def retrieve_repo_context(query: str, top_k: int = 8) -> dict:
    return _retrieve_repo_context(query=query, top_k=top_k)


def read_repo_file(path: str, start_line: int = 1, end_line: int = 200) -> dict:
    return _read_repo_file(path=path, start_line=start_line, end_line=end_line)


def safe_calculate(expression: str) -> dict:
    return _safe_calculate(expression=expression)


def write_brief(filename: str, content: str) -> dict:
    return _write_brief(filename=filename, content=content)


def generate_grafana_dashboard(
    title: str,
    datasource: str = "Prometheus",
    panels: list[dict[str, Any]] | None = None,
    filename: str | None = None,
    templating: list[dict[str, Any]] | None = None,
    variable_filters: dict[str, str] | None = None,
) -> dict:
    return _generate_grafana_dashboard(
        title=title,
        datasource=datasource,
        panels=panels,
        filename=filename,
        templating=templating,
        variable_filters=variable_filters,
    )


def generate_grafana_alert_rule(
    title: str,
    expr: str,
    datasource_uid: str = "default",
    for_duration: str = "5m",
    severity: str = "warning",
    filename: str | None = None,
) -> dict:
    return _generate_grafana_alert_rule(
        title=title,
        expr=expr,
        datasource_uid=datasource_uid,
        for_duration=for_duration,
        severity=severity,
        filename=filename,
    )


def generate_prompt_template(
    name: str,
    purpose: str,
    instructions: str,
    inputs: list[str] | None = None,
    filename: str | None = None,
) -> dict:
    return _generate_prompt_template(
        name=name,
        purpose=purpose,
        instructions=instructions,
        inputs=inputs,
        filename=filename,
    )


def add_panel_to_dashboard(
    filename: str,
    panel_title: str,
    expr: str,
    panel_type: str = "timeseries",
    datasource: str = "Prometheus",
) -> dict:
    return _add_panel_to_dashboard(
        filename=filename,
        panel_title=panel_title,
        expr=expr,
        panel_type=panel_type,
        datasource=datasource,
    )


def check_prometheus_metrics(
    metrics: list[str] | None = None,
    exprs: list[str] | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 4.0,
) -> dict[str, Any]:
    return _check_prometheus_metrics(
        metrics=metrics,
        exprs=exprs,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
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
    return _lint_grafana_dashboard(
        filename=filename,
        auto_fix=auto_fix,
        enforce_no_templating=enforce_no_templating,
        required_datasource_uid=required_datasource_uid,
        profile=profile,
        check_metric_availability=check_metric_availability,
        prometheus_base_url=prometheus_base_url,
    )


def patch_grafana_dashboard(
    filename: str,
    instructions: str = "",
    output_filename: str | None = None,
    profile: str = "generic",
    auto_fix: bool = True,
    enforce_no_templating: bool = True,
    required_datasource_uid: str = "default",
    create_diff: bool = True,
) -> dict[str, Any]:
    return _patch_grafana_dashboard(
        filename=filename,
        instructions=instructions,
        output_filename=output_filename,
        profile=profile,
        auto_fix=auto_fix,
        enforce_no_templating=enforce_no_templating,
        required_datasource_uid=required_datasource_uid,
        create_diff=create_diff,
    )


def run_agent(
    question: str,
    allow_actions: bool = True,
    session_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Lazy import prevents circular dependency when runtime base imports tool exports.
    from runtime_engine.runner import run_agent as _run_agent

    return _run_agent(
        question=question,
        allow_actions=allow_actions,
        session_context=session_context,
    )
