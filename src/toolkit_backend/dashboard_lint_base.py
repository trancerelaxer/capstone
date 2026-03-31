"""Dashboard linting support utilities and panel/grid validators."""

from toolkit_backend.generation import *  # noqa: F401,F403
from toolkit_backend.base import _resolve_output_file_path  # noqa: F401
from toolkit_backend.generation import _build_dashboard_panel  # noqa: F401
from toolkit_backend.promql import _sanitize_panel_type  # noqa: F401


def _resolve_output_dashboard_path(filename: str) -> Path:
    safe_name = Path(str(filename or "")).name
    if not safe_name:
        raise ValueError("filename is required")
    return _resolve_output_file_path(
        safe_name, migrate_legacy_to_canonical=True
    )


def _extract_panel_specs_from_dashboard(panels: list[Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        title = str(panel.get("title", "")).strip()
        panel_type = _sanitize_panel_type(panel.get("type", "timeseries"))
        targets = panel.get("targets", [])
        expr = ""
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, dict) and isinstance(target.get("expr"), str):
                    expr = str(target.get("expr", "")).strip()
                    if expr:
                        break
        if not title or not expr:
            continue
        specs.append({"title": title, "type": panel_type, "expr": expr})
    return specs


def _panel_grid_errors(panels: list[Any]) -> list[str]:
    errors: list[str] = []
    rectangles: list[tuple[int, int, int, int, int]] = []

    for idx, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            errors.append(f"Panel #{idx}: panel must be an object.")
            continue
        grid = panel.get("gridPos")
        if not isinstance(grid, dict):
            errors.append(f"Panel #{idx}: missing gridPos.")
            continue
        try:
            x = int(grid.get("x", 0))
            y = int(grid.get("y", 0))
            w = int(grid.get("w", 0))
            h = int(grid.get("h", 0))
        except Exception:
            errors.append(f"Panel #{idx}: gridPos values must be integers.")
            continue
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            errors.append(f"Panel #{idx}: gridPos has invalid dimensions.")
            continue
        if x + w > 24:
            errors.append(f"Panel #{idx}: gridPos exceeds 24-column width.")
            continue
        rectangles.append((x, y, x + w, y + h, idx))

    for i in range(len(rectangles)):
        ax1, ay1, ax2, ay2, aidx = rectangles[i]
        for j in range(i + 1, len(rectangles)):
            bx1, by1, bx2, by2, bidx = rectangles[j]
            overlap_x = ax1 < bx2 and bx1 < ax2
            overlap_y = ay1 < by2 and by1 < ay2
            if overlap_x and overlap_y:
                errors.append(f"Panels #{aidx} and #{bidx} overlap in gridPos.")
    return errors


def _rebuild_panels_from_specs(
    panel_specs: list[dict[str, Any]],
    datasource: str,
    datasource_uid: str,
) -> list[dict[str, Any]]:
    rebuilt: list[dict[str, Any]] = []
    for idx, spec in enumerate(panel_specs, start=1):
        enriched = dict(spec)
        enriched["datasource_uid"] = datasource_uid
        enriched.pop("gridPos", None)
        rebuilt.append(
            _build_dashboard_panel(
                panel_index=idx,
                panel_count=len(panel_specs),
                spec=enriched,
                datasource=datasource,
                variable_filters=None,
            )
        )
    return rebuilt
