"""Tool execution pipeline, context assembly, and self-check helpers."""

from runtime_engine.planner import *  # noqa: F401,F403
from runtime_engine.base import (
    _ensure_kubernetes_pod_panels,
    _looks_like_kubernetes_pod_dashboard_request,
)  # noqa: F401


def _execute_tools(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for call in tool_calls:
        name = call.get("tool")
        args = call.get("arguments", {})
        if name not in TOOLS:
            outputs.append({"tool": name, "error": "Unknown tool"})
            continue
        if not isinstance(args, dict):
            outputs.append({"tool": name, "error": "Arguments must be an object"})
            continue
        try:
            result = TOOLS[name](**args)
            outputs.append({"tool": name, "arguments": args, "result": result})
        except Exception as exc:
            outputs.append({"tool": name, "arguments": args, "error": str(exc)})
    return outputs


def _build_context_from_tools(tool_outputs: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for out in tool_outputs:
        if out.get("tool") not in {
            "retrieve_context",
            "retrieve_repo_context",
            "read_repo_file",
        }:
            continue
        result = out.get("result", {})
        if not isinstance(result, dict):
            continue
        current_chunks = result.get("chunks", [])
        if isinstance(current_chunks, list):
            chunks.extend([str(c) for c in current_chunks])
    if not chunks:
        return ""
    context = "\n\n---\n\n".join(chunks)
    return context[:MAX_CONTEXT_CHARS]


def _has_retrieved_context(tool_outputs: list[dict[str, Any]]) -> bool:
    for out in tool_outputs:
        if out.get("tool") not in {
            "retrieve_context",
            "retrieve_repo_context",
            "read_repo_file",
        }:
            continue
        result = out.get("result", {})
        if not isinstance(result, dict):
            continue
        chunk_count = int(result.get("chunk_count", 0) or 0)
        chunks = result.get("chunks", [])
        if chunk_count > 0 or (isinstance(chunks, list) and len(chunks) > 0):
            return True
    return False


def _no_context_answer(question: str) -> str:
    return (
        "I could not find relevant context in the indexed Grafana docs or repository context for this request.\n"
        f"Question: {question[:MAX_QUESTION_CHARS]}\n\n"
        "Please try one of these:\n"
        "- Ask a more specific question with file/module names for repo analysis.\n"
        "- Re-run docs ingestion (`ingest-docs --force`) to refresh docs index.\n"
        "- Ask me to generate a JSON artifact directly (dashboard/alert/prompt template).\n"
        "Confidence: Low"
    )


def _summarize_tool_outputs(tool_outputs: list[dict[str, Any]]) -> str:
    compact: list[dict[str, Any]] = []
    for out in tool_outputs:
        tool_name = out.get("tool")
        if tool_name in {"retrieve_context", "retrieve_repo_context"}:
            result = out.get("result", {})
            if isinstance(result, dict):
                compact.append(
                    {
                        "tool": tool_name,
                        "query": result.get("query", ""),
                        "top_k": result.get("top_k", 0),
                        "chunk_count": result.get("chunk_count", 0),
                        "retrieval_mode": result.get("retrieval_mode", ""),
                    }
                )
            else:
                compact.append({"tool": tool_name, "status": "no_result"})
            continue
        if tool_name == "read_repo_file":
            result = out.get("result", {})
            if isinstance(result, dict):
                compact.append(
                    {
                        "tool": "read_repo_file",
                        "path": result.get("path", ""),
                        "start_line": result.get("start_line", 0),
                        "end_line": result.get("end_line", 0),
                        "line_count": result.get("line_count", 0),
                    }
                )
            else:
                compact.append({"tool": "read_repo_file", "status": "no_result"})
            continue
        entry = {"tool": tool_name}
        if "error" in out:
            entry["error"] = out.get("error")
        else:
            result = out.get("result", {})
            if isinstance(result, dict):
                entry["result_keys"] = list(result.keys())[:8]
            else:
                entry["result_type"] = type(result).__name__
        compact.append(entry)
    return json.dumps(compact, ensure_ascii=True)[:MAX_TOOL_SUMMARY_CHARS]


def _extract_artifacts(tool_outputs: list[dict[str, Any]]) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for out in tool_outputs:
        tool_name = str(out.get("tool", ""))
        result = out.get("result", {})
        if not isinstance(result, dict):
            continue
        saved_to = result.get("saved_to")
        if not isinstance(saved_to, str):
            file_name = ""
        else:
            file_name = os.path.basename(saved_to)
        if file_name:
            if tool_name in {
                "generate_grafana_dashboard",
                "patch_grafana_dashboard",
                "add_panel_to_dashboard",
            }:
                artifact_type = "dashboard_json"
            elif tool_name == "generate_grafana_alert_rule":
                artifact_type = "alert_json"
            elif tool_name == "generate_prompt_template":
                artifact_type = "prompt_json"
            else:
                artifact_type = "file"
            artifacts.append({"type": artifact_type, "name": file_name})

        if tool_name == "patch_grafana_dashboard":
            diff_file = str(result.get("diff_file", "")).strip()
            if diff_file:
                artifacts.append({"type": "diff", "name": os.path.basename(diff_file)})
    return artifacts


def _build_generation_answer(tool_outputs: list[dict[str, Any]]) -> str | None:
    generated: list[tuple[str, str]] = []
    lint_results: list[dict[str, Any]] = []
    for out in tool_outputs:
        tool_name = str(out.get("tool", ""))
        if tool_name == "lint_grafana_dashboard":
            result = out.get("result", {})
            if isinstance(result, dict):
                lint_results.append(result)
            continue
        if tool_name not in {
            "generate_grafana_dashboard",
            "generate_grafana_alert_rule",
            "generate_prompt_template",
            "patch_grafana_dashboard",
            "write_brief",
        }:
            continue
        result = out.get("result", {})
        if not isinstance(result, dict):
            continue
        saved_to = result.get("saved_to")
        if not isinstance(saved_to, str):
            continue
        file_name = os.path.basename(saved_to)
        if not file_name:
            continue
        generated.append((tool_name, file_name))

    if not generated:
        return None

    lines = ["Generated artifact(s):"]
    for tool_name, file_name in generated:
        if tool_name == "generate_grafana_dashboard":
            label = "Grafana dashboard JSON"
        elif tool_name == "patch_grafana_dashboard":
            label = "Patched dashboard JSON"
        elif tool_name == "generate_grafana_alert_rule":
            label = "Grafana alert JSON"
        elif tool_name == "generate_prompt_template":
            label = "Prompt template JSON"
        else:
            label = "File"
        lines.append(f"- {label}: {file_name}")
        if tool_name == "patch_grafana_dashboard":
            for out in tool_outputs:
                if str(out.get("tool", "")) != "patch_grafana_dashboard":
                    continue
                result = out.get("result", {})
                if not isinstance(result, dict):
                    continue
                if (
                    os.path.basename(str(result.get("saved_to", "")).strip())
                    != file_name
                ):
                    continue
                diff_file = str(result.get("diff_file", "")).strip()
                diff_line_count = int(result.get("diff_line_count", 0) or 0)
                if diff_file:
                    lines.append(
                        f"- Patch diff: {os.path.basename(diff_file)} ({diff_line_count} lines)"
                    )
                break
    if lint_results:
        final_lint = lint_results[-1]
        lint_ok = bool(final_lint.get("ok", False))
        fixed_count = int(final_lint.get("fixed_count", 0) or 0)
        if lint_ok:
            lines.append(f"Validation: Passed (auto-fixes applied: {fixed_count}).")
        else:
            issues = final_lint.get("issues", [])
            issue_count = len(issues) if isinstance(issues, list) else 0
            lines.append(f"Validation: Issues remain ({issue_count}).")
    lines.append("Use the download link in the chat UI to get the file.")
    confidence = "High"
    if lint_results and not bool(lint_results[-1].get("ok", False)):
        confidence = "Medium"
    lines.append(f"Confidence: {confidence}")
    return "\n".join(lines)


def _build_dashboard_update_answer(tool_outputs: list[dict[str, Any]]) -> str | None:
    updates: list[tuple[str, str]] = []
    for out in tool_outputs:
        if str(out.get("tool", "")) != "add_panel_to_dashboard":
            continue
        result = out.get("result", {})
        if not isinstance(result, dict):
            continue
        saved_to = str(result.get("saved_to", "")).strip()
        dashboard_file = str(result.get("dashboard_file", "")).strip()
        panel_title = str(result.get("panel_title", "")).strip() or "New Panel"
        file_name = os.path.basename(saved_to) if saved_to else dashboard_file
        if not file_name:
            continue
        updates.append((file_name, panel_title))

    if not updates:
        return None

    lines = ["Dashboard updated:"]
    for file_name, panel_title in updates:
        lines.append(f"- File: {file_name}")
        lines.append(f"- Added panel: {panel_title}")
    lines.append("Use the download link in the chat UI to get the updated file.")
    lines.append("Confidence: High")
    return "\n".join(lines)


def _dashboard_file_from_result(result: dict[str, Any]) -> str:
    saved_to = str(result.get("saved_to", "")).strip()
    if saved_to:
        file_name = os.path.basename(saved_to)
        if file_name:
            return file_name
    dashboard_file = str(result.get("dashboard_file", "")).strip()
    return os.path.basename(dashboard_file) if dashboard_file else ""


def _latest_dashboard_file_from_outputs(tool_outputs: list[dict[str, Any]]) -> str:
    for out in reversed(tool_outputs):
        tool_name = str(out.get("tool", ""))
        if tool_name not in {
            "generate_grafana_dashboard",
            "add_panel_to_dashboard",
            "lint_grafana_dashboard",
        }:
            continue
        result = out.get("result", {})
        if not isinstance(result, dict):
            continue
        file_name = _dashboard_file_from_result(result)
        if file_name:
            return file_name
    return ""


def _generation_retry_args(
    question: str,
    initial_calls: list[dict[str, Any]],
    profile: str,
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for call in initial_calls:
        if str(call.get("tool", "")) != "generate_grafana_dashboard":
            continue
        call_args = call.get("arguments", {})
        if isinstance(call_args, dict):
            args = dict(call_args)
            break

    title = str(args.get("title", "")).strip() or "Generated Dashboard"
    datasource = str(args.get("datasource", "")).strip() or "Prometheus"
    retry_args: dict[str, Any] = {"title": title, "datasource": datasource}

    panels = args.get("panels")
    if profile == "kubernetes_pods":
        panels_list = panels if isinstance(panels, list) else []
        retry_args["panels"] = _ensure_kubernetes_pod_panels(panels_list)
        if title.lower() == "generated dashboard":
            retry_args["title"] = "Kubernetes Pod Monitoring"
    elif isinstance(panels, list) and panels:
        retry_args["panels"] = panels

    filename = str(args.get("filename", "")).strip()
    if filename:
        retry_args["filename"] = filename
    else:
        lowered = question.lower()
        if "kubernetes" in lowered or "k8s" in lowered:
            retry_args["filename"] = "kubernetes-pod-monitoring.dashboard.json"
    return retry_args


def _run_dashboard_self_check(
    question: str,
    request_mode: str,
    initial_calls: list[dict[str, Any]],
    tool_outputs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if request_mode not in {"generate_dashboard", "update_dashboard"}:
        return tool_outputs, False

    dashboard_file = _latest_dashboard_file_from_outputs(tool_outputs)
    if not dashboard_file:
        return tool_outputs, False

    profile = (
        "kubernetes_pods"
        if _looks_like_kubernetes_pod_dashboard_request(question)
        else "generic"
    )
    lint_calls = [
        {
            "tool": "lint_grafana_dashboard",
            "arguments": {
                "filename": dashboard_file,
                "auto_fix": True,
                "enforce_no_templating": True,
                "required_datasource_uid": "default",
                "profile": profile,
            },
        }
    ]
    lint_outputs = _execute_tools(lint_calls)
    tool_outputs.extend(lint_outputs)

    lint_result = lint_outputs[0].get("result", {}) if lint_outputs else {}
    lint_ok = isinstance(lint_result, dict) and bool(lint_result.get("ok", False))
    if lint_ok:
        return tool_outputs, False

    if request_mode != "generate_dashboard":
        return tool_outputs, False

    # Retry generation once with stronger defaults, then lint again.
    retry_args = _generation_retry_args(
        question=question, initial_calls=initial_calls, profile=profile
    )
    retry_calls = [{"tool": "generate_grafana_dashboard", "arguments": retry_args}]
    retry_outputs = _execute_tools(retry_calls)
    tool_outputs.extend(retry_outputs)

    retry_dashboard_file = _latest_dashboard_file_from_outputs(
        retry_outputs
    ) or _latest_dashboard_file_from_outputs(tool_outputs)
    if retry_dashboard_file:
        second_lint_calls = [
            {
                "tool": "lint_grafana_dashboard",
                "arguments": {
                    "filename": retry_dashboard_file,
                    "auto_fix": True,
                    "enforce_no_templating": True,
                    "required_datasource_uid": "default",
                    "profile": profile,
                },
            }
        ]
        second_lint_outputs = _execute_tools(second_lint_calls)
        tool_outputs.extend(second_lint_outputs)

    return tool_outputs, True
