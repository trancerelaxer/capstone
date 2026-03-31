"""Request-mode detection and tool-call planning."""

from runtime_engine.spec_inference import *  # noqa: F401,F403
from runtime_engine.base import (
    _chat,
    _extract_json,
    _looks_like_kubernetes_pod_dashboard_request,
)  # noqa: F401
from runtime_engine.spec_inference import (  # noqa: F401
    _fallback_dashboard_generation_spec,
    _infer_alert_generation_spec,
    _infer_dashboard_generation_spec,
    _infer_prompt_template_spec,
)


def _plan_tools(question: str, allowed_tools: set[str]) -> list[dict[str, Any]]:
    question = question[:MAX_QUESTION_CHARS]
    allowed_tool_names = [name for name in TOOLS if name in allowed_tools]
    preferred_retrieval_tool = (
        "retrieve_context"
        if "retrieve_context" in allowed_tool_names
        else "retrieve_repo_context"
    )
    compact_tools = [
        {"name": t["name"], "purpose": t["purpose"]}
        for t in TOOL_DESCRIPTIONS
        if t["name"] in allowed_tool_names
    ]
    if not compact_tools:
        compact_tools = [
            {
                "name": "retrieve_context",
                "purpose": "Retrieve relevant knowledge chunks for grounding.",
            }
        ]
        allowed_tool_names = ["retrieve_context"]

    prompt = f"""You are an AI agent planner.
Question: {question}

Available tools (compact):
{json.dumps(compact_tools, ensure_ascii=True)}

Rules:
- Choose 1-3 tool calls maximum.
- First tool should usually be {preferred_retrieval_tool}.
- Use only these tool names: {json.dumps(allowed_tool_names, ensure_ascii=True)}
- Output ONLY JSON with this shape:
{{
  "tool_calls": [
    {{"tool": "tool_name", "arguments": {{...}}}}
  ]
}}"""
    raw = _chat(prompt, temperature=0.1)
    try:
        parsed = _extract_json(raw)
        tool_calls = parsed.get("tool_calls", [])
        if isinstance(tool_calls, list):
            return tool_calls[:3]
    except Exception:
        pass
    return [
        {
            "tool": "retrieve_context",
            "arguments": {"query": question, "top_k": DEFAULT_TOP_K},
        }
    ]


def _direct_generation_plan(
    question: str,
    allow_actions: bool,
    request_mode: str | None = None,
) -> list[dict[str, Any]] | None:
    if not allow_actions:
        return None

    if request_mode not in {"generate_dashboard", "generate_alert", "generate_prompt"}:
        return None

    if request_mode == "generate_dashboard":
        # Keep dashboard generation fully agentic (LLM-inferred), then rely on
        # generator-side normalization/safety filters for correctness.
        spec = _infer_dashboard_generation_spec(question)
        if spec is None:
            spec = _fallback_dashboard_generation_spec(question)
        args: dict[str, Any] = {
            "title": spec.get("title", "Generated Dashboard"),
            "datasource": spec.get("datasource", "Prometheus"),
        }
        panels = spec.get("panels")
        if isinstance(panels, list) and panels:
            args["panels"] = panels
        return [
            {
                "tool": "generate_grafana_dashboard",
                "arguments": args,
            }
        ]

    if request_mode == "generate_alert":
        spec = _infer_alert_generation_spec(question)
        if spec is None:
            return None
        return [
            {
                "tool": "generate_grafana_alert_rule",
                "arguments": spec,
            }
        ]

    if request_mode == "generate_prompt":
        spec = _infer_prompt_template_spec(question)
        if spec is None:
            return None
        return [
            {
                "tool": "generate_prompt_template",
                "arguments": spec,
            }
        ]

    return None


def _direct_repo_analysis_plan(
    question: str, request_mode: str | None = None
) -> list[dict[str, Any]] | None:
    if request_mode != "repo_qa":
        return None
    calls: list[dict[str, Any]] = [
        {
            "tool": "retrieve_repo_context",
            "arguments": {
                "query": question[:MAX_QUESTION_CHARS],
                "top_k": DEFAULT_TOP_K + 2,
            },
        }
    ]
    path_match = re.search(
        r"([a-zA-Z0-9_./-]+\.(?:py|md|mmd|json|yaml|yml|toml|txt|html|js|ts|css))",
        question,
    )
    if path_match:
        calls.append(
            {
                "tool": "read_repo_file",
                "arguments": {
                    "path": path_match.group(1),
                    "start_line": 1,
                    "end_line": 220,
                },
            }
        )
    return calls


def _infer_request_mode(question: str, session_context: dict[str, Any] | None) -> str:
    has_dashboard_context = False
    if isinstance(session_context, dict):
        has_dashboard_context = bool(
            str(session_context.get("latest_dashboard_file", "")).strip()
        )
    lowered_question = question.lower()
    has_explicit_dashboard_file = (
        re.search(r"[a-zA-Z0-9_.-]+\.dashboard\.json", question, flags=re.IGNORECASE)
        is not None
    )
    has_update_target = has_dashboard_context or has_explicit_dashboard_file
    has_patch_context = has_dashboard_context or has_explicit_dashboard_file
    repo_markers = (
        "repo",
        "repository",
        "codebase",
        "source code",
        "this project",
        "this code",
        "what are we building",
        "how is this implemented",
        "how does this work",
    )
    generation_markers = (
        "generate dashboard",
        "create dashboard",
        "build dashboard",
        "generate alert",
        "create alert",
        "build alert",
        "prompt template",
    )
    patch_markers = (
        "fix this file",
        "fix this dashboard",
        "patch dashboard",
        "repair dashboard",
        "lint dashboard",
        "validate dashboard",
    )
    update_markers = (
        "add panel",
        "add a panel",
        "new panel",
        "append panel",
        "update dashboard",
        "modify dashboard",
        "edit dashboard",
    )
    if has_update_target and any(
        marker in lowered_question for marker in update_markers
    ):
        return "update_dashboard"
    if has_patch_context and any(
        marker in lowered_question for marker in patch_markers
    ):
        return "patch_dashboard"
    if (
        "src/" in lowered_question
        or ".py" in lowered_question
        or any(marker in lowered_question for marker in repo_markers)
    ) and not any(marker in lowered_question for marker in generation_markers):
        return "repo_qa"
    if any(
        token in lowered_question
        for token in (
            "generate dashboard",
            "create dashboard",
            "build dashboard",
            "dashboard json",
        )
    ):
        return "generate_dashboard"
    if any(
        token in lowered_question
        for token in ("generate alert", "create alert", "build alert", "alert json")
    ):
        return "generate_alert"
    if "prompt template" in lowered_question:
        return "generate_prompt"

    prompt = f"""Classify this user request for a Grafana assistant.
Return JSON only:
{{
  "mode": "qa|repo_qa|generate_dashboard|generate_alert|generate_prompt|update_dashboard|patch_dashboard"
}}

Rules:
- "repo_qa": user asks about this repository/codebase/files/implementation/architecture.
- "generate_dashboard": user asks to create/build/make dashboard JSON/file.
- "generate_alert": user asks to create/build/make alert JSON/file.
- "generate_prompt": user asks to create/build/make prompt template JSON/file.
- "update_dashboard": user asks to add/modify panel/graph on an existing dashboard and context exists.
- "patch_dashboard": user asks to fix/repair/validate an existing dashboard file and context exists.
- otherwise "qa".

Context:
- has_dashboard_context={str(has_dashboard_context).lower()}
- has_patch_context={str(has_patch_context).lower()}

User request:
{question[:MAX_QUESTION_CHARS]}
"""
    try:
        raw = _chat(prompt, temperature=0)
        parsed = _extract_json(raw)
        mode = str(parsed.get("mode", "")).strip()
        valid_modes = {
            "qa",
            "repo_qa",
            "generate_dashboard",
            "generate_alert",
            "generate_prompt",
            "update_dashboard",
            "patch_dashboard",
        }
        if mode in valid_modes:
            if mode == "update_dashboard" and not has_update_target:
                return "qa"
            if mode == "patch_dashboard" and not has_patch_context:
                return "qa"
            return mode
    except Exception:
        return "qa"
    return "qa"


def _infer_panel_spec(question: str) -> dict[str, str] | None:
    question = question[:MAX_QUESTION_CHARS]
    prompt = f"""You convert user intent into a Grafana panel spec for Prometheus.
Return JSON only with this exact schema:
{{
  "panel_title": "string",
  "expr": "string",
  "panel_type": "timeseries|barchart|stat",
  "datasource": "Prometheus"
}}

Rules:
- Generate exactly one practical panel.
- If user asks for a specific HTTP status code (e.g., 400/404/500), use that exact code in the PromQL filter.
- If user asks for status class (4xx/5xx), use status=~"4.." or status=~"5..".
- If user mentions pods/kubernetes, prefer grouping by namespace and pod when relevant.
- Output JSON only.

User request:
{question}
"""
    try:
        raw = _chat(prompt, temperature=0)
        parsed = _extract_json(raw)
        panel_title = str(parsed.get("panel_title", "")).strip()
        expr = str(parsed.get("expr", "")).strip()
        panel_type = str(parsed.get("panel_type", "timeseries")).strip() or "timeseries"
        datasource = str(parsed.get("datasource", "Prometheus")).strip() or "Prometheus"
        if panel_title and expr:
            return {
                "panel_title": panel_title[:120],
                "expr": expr[:500],
                "panel_type": panel_type[:40],
                "datasource": datasource[:80],
            }
    except Exception:
        return None
    return None


def _fallback_panel_spec(question: str) -> dict[str, str]:
    lowered = question.lower()
    if any(token in lowered for token in ("traffic", "network", "bandwidth", "throughput")):
        return {
            "panel_title": "Pod Network Traffic (bytes/s)",
            "expr": 'sum(rate(container_network_receive_bytes_total{namespace!="",pod!=""}[5m]) + rate(container_network_transmit_bytes_total{namespace!="",pod!=""}[5m])) by (namespace, pod)',
            "panel_type": "timeseries",
            "datasource": "Prometheus",
        }
    return {
        "panel_title": "Pod Health",
        "expr": 'sum(kube_pod_status_phase{phase="Running",namespace!="",pod!=""}) by (namespace)',
        "panel_type": "barchart",
        "datasource": "Prometheus",
    }


def _allowed_tools_for_request(
    request_mode: str,
    allow_actions: bool,
) -> set[str]:
    if request_mode == "repo_qa":
        allowed = {"retrieve_repo_context", "read_repo_file", "safe_calculate"}
    else:
        allowed = {"retrieve_context", "safe_calculate", "check_prometheus_metrics"}
    if not allow_actions:
        return allowed
    if request_mode in {"generate_dashboard", "generate_alert", "generate_prompt"}:
        allowed.update(ARTIFACT_TOOLS)
    if request_mode in {"generate_dashboard", "update_dashboard", "patch_dashboard"}:
        allowed.add("lint_grafana_dashboard")
    if request_mode == "update_dashboard":
        allowed.update(UPDATE_TOOLS)
    if request_mode == "patch_dashboard":
        allowed.add("patch_grafana_dashboard")
    return allowed


def _sanitize_tool_calls(
    tool_calls: list[dict[str, Any]], allowed_tools: set[str], question: str
) -> list[dict[str, Any]]:
    sanitized_calls: list[dict[str, Any]] = []
    question = question[:MAX_QUESTION_CHARS]

    for call in tool_calls[:3]:
        if not isinstance(call, dict):
            continue
        name = str(call.get("tool", ""))
        if name not in TOOLS or name not in allowed_tools:
            continue
        raw_args = call.get("arguments", {})
        args = raw_args if isinstance(raw_args, dict) else {}
        if name == "retrieve_context":
            query = str(args.get("query") or question).strip()
            try:
                top_k = int(args.get("top_k", DEFAULT_TOP_K))
            except Exception:
                top_k = DEFAULT_TOP_K
            args = {
                "query": query[:MAX_QUESTION_CHARS],
                "top_k": max(1, min(top_k, 20)),
            }
        if name == "retrieve_repo_context":
            query = str(args.get("query") or question).strip()
            try:
                top_k = int(args.get("top_k", DEFAULT_TOP_K + 2))
            except Exception:
                top_k = DEFAULT_TOP_K + 2
            args = {
                "query": query[:MAX_QUESTION_CHARS],
                "top_k": max(1, min(top_k, 20)),
            }
        if name == "read_repo_file":
            path = str(args.get("path", "")).strip()
            if not path:
                continue
            try:
                start_line = int(args.get("start_line", 1))
            except Exception:
                start_line = 1
            try:
                end_line = int(args.get("end_line", 200))
            except Exception:
                end_line = 200
            start_line = max(1, start_line)
            end_line = max(start_line, min(end_line, start_line + 400))
            args = {"path": path[:260], "start_line": start_line, "end_line": end_line}
        sanitized_calls.append({"tool": name, "arguments": args})

    if sanitized_calls:
        return sanitized_calls
    fallback_tool = "retrieve_context"
    if (
        "retrieve_repo_context" in allowed_tools
        and "retrieve_context" not in allowed_tools
    ):
        fallback_tool = "retrieve_repo_context"
    return [
        {
            "tool": fallback_tool,
            "arguments": {"query": question, "top_k": DEFAULT_TOP_K},
        }
    ]


def _direct_update_plan(
    question: str,
    session_context: dict[str, Any] | None,
    allow_actions: bool,
    request_mode: str | None = None,
) -> list[dict[str, Any]] | None:
    if not allow_actions:
        return None
    if request_mode != "update_dashboard":
        return None

    latest_dashboard = (
        str(session_context.get("latest_dashboard_file", "")).strip()
        if session_context
        else ""
    )
    if not latest_dashboard:
        file_match = re.search(
            r"([a-zA-Z0-9_.-]+\.dashboard\.json)", question, flags=re.IGNORECASE
        )
        if file_match:
            latest_dashboard = file_match.group(1)
    if not latest_dashboard:
        return None

    panel_spec = _infer_panel_spec(question)
    if panel_spec is None:
        panel_spec = _fallback_panel_spec(question)
    panel_title = panel_spec.get("panel_title", "Custom Panel")
    expr = panel_spec.get("expr", "up")
    panel_type = panel_spec.get("panel_type", "timeseries")
    datasource = panel_spec.get("datasource", "Prometheus")

    return [
        {
            "tool": "add_panel_to_dashboard",
            "arguments": {
                "filename": latest_dashboard,
                "panel_title": panel_title,
                "expr": expr,
                "panel_type": panel_type,
                "datasource": datasource,
            },
        }
    ]


def _direct_patch_plan(
    question: str,
    session_context: dict[str, Any] | None,
    allow_actions: bool,
    request_mode: str | None = None,
) -> list[dict[str, Any]] | None:
    if not allow_actions:
        return None
    if request_mode != "patch_dashboard":
        return None

    latest_dashboard = (
        str(session_context.get("latest_dashboard_file", "")).strip()
        if session_context
        else ""
    )
    file_match = re.search(
        r"([a-zA-Z0-9_.-]+\.dashboard\.json)", question, flags=re.IGNORECASE
    )
    filename = file_match.group(1) if file_match else latest_dashboard
    if not filename:
        return None

    save_as_match = re.search(
        r"save\s+as\s+([a-zA-Z0-9_.-]+\.json)", question, flags=re.IGNORECASE
    )
    output_filename = save_as_match.group(1) if save_as_match else ""
    profile = (
        "kubernetes_pods"
        if _looks_like_kubernetes_pod_dashboard_request(question)
        else "generic"
    )

    args: dict[str, Any] = {
        "filename": filename,
        "instructions": question[:MAX_QUESTION_CHARS],
        "profile": profile,
    }
    if output_filename:
        args["output_filename"] = output_filename

    return [{"tool": "patch_grafana_dashboard", "arguments": args}]
