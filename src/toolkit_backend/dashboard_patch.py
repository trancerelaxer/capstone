"""Dashboard patch workflow and diff artifact generation."""

from toolkit_backend.dashboard_lint import *  # noqa: F401,F403
from toolkit_backend.dashboard_lint import (
    _extract_required_uid,
    _infer_lint_profile,
)  # noqa: F401
from toolkit_backend.dashboard_lint_base import (
    _resolve_output_dashboard_path,
)  # noqa: F401


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
    source_path = _resolve_output_dashboard_path(filename)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Dashboard file not found: {Path(filename).name}")

    target_name = Path(output_filename).name if output_filename else source_path.name
    if not target_name:
        target_name = source_path.name
    target_path = OUTPUT_DIR / target_name

    original_text = source_path.read_text(encoding="utf-8")
    if target_path.resolve() != source_path.resolve():
        target_path.write_text(original_text, encoding="utf-8")

    effective_profile = _infer_lint_profile(instructions, fallback_profile=profile)
    effective_uid = _extract_required_uid(
        instructions, default_uid=required_datasource_uid
    )
    lint_result = lint_grafana_dashboard(
        filename=target_path.name,
        auto_fix=auto_fix,
        enforce_no_templating=enforce_no_templating,
        required_datasource_uid=effective_uid,
        profile=effective_profile,
    )

    updated_text = target_path.read_text(encoding="utf-8")
    diff_lines = list(
        difflib.unified_diff(
            original_text.splitlines(),
            updated_text.splitlines(),
            fromfile=source_path.name,
            tofile=target_path.name,
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines).strip()
    diff_file_name = f"{target_path.stem}.patch.diff"
    diff_path = OUTPUT_DIR / diff_file_name
    if create_diff:
        diff_path.write_text((diff_text + "\n") if diff_text else "", encoding="utf-8")

    return {
        "saved_to": str(target_path),
        "dashboard_file": target_path.name,
        "diff_file": diff_file_name if create_diff else "",
        "diff_line_count": len(diff_lines),
        "diff_preview": diff_text[:4000],
        "changed": original_text != updated_text,
        "instructions": str(instructions or "").strip(),
        "lint": lint_result,
    }
