"""Dashboard quality, linting, metric probing, and patch tools."""

from agent import (  # noqa: F401
    check_prometheus_metrics,
    lint_grafana_dashboard,
    patch_grafana_dashboard,
)
