"""Final synthesis/reflection and main run_agent orchestration."""

from runtime_engine.pipeline import *  # noqa: F401,F403
from runtime_engine.base import _chat, _extract_json  # noqa: F401
from runtime_engine.pipeline import (  # noqa: F401
    _build_context_from_tools,
    _build_dashboard_update_answer,
    _build_generation_answer,
    _execute_tools,
    _extract_artifacts,
    _has_retrieved_context,
    _no_context_answer,
    _run_dashboard_self_check,
    _summarize_tool_outputs,
)
from runtime_engine.planner import (  # noqa: F401
    _allowed_tools_for_request,
    _direct_generation_plan,
    _direct_patch_plan,
    _direct_repo_analysis_plan,
    _direct_update_plan,
    _infer_request_mode,
    _plan_tools,
    _sanitize_tool_calls,
)


def _reflect(question: str, answer: str, context: str) -> dict[str, Any]:
    question = question[:MAX_QUESTION_CHARS]
    prompt = f"""You are a strict evaluator for an agent answer.
Return JSON only:
{{
  "groundedness_score": 1-5,
  "completeness_score": 1-5,
  "actionability_score": 1-5,
  "need_retry": true/false,
  "improvement_hint": "string"
}}

Question:
{question}

Context used:
{context[:MAX_REFLECT_CONTEXT_CHARS]}

Answer:
{answer[:MAX_REFLECT_ANSWER_CHARS]}
"""
    raw = _chat(prompt, temperature=0)
    try:
        parsed = _extract_json(raw)
        return {
            "groundedness_score": int(parsed.get("groundedness_score", 3)),
            "completeness_score": int(parsed.get("completeness_score", 3)),
            "actionability_score": int(parsed.get("actionability_score", 3)),
            "need_retry": bool(parsed.get("need_retry", False)),
            "improvement_hint": str(parsed.get("improvement_hint", "")),
        }
    except Exception:
        return {
            "groundedness_score": 3,
            "completeness_score": 3,
            "actionability_score": 3,
            "need_retry": False,
            "improvement_hint": "",
        }


def _synthesize_answer(
    question: str, tool_outputs: list[dict[str, Any]], reflection_hint: str = ""
) -> str:
    question = question[:MAX_QUESTION_CHARS]
    context = _build_context_from_tools(tool_outputs)
    tool_summary = _summarize_tool_outputs(tool_outputs)
    prompt = f"""You are a self-reflective RAG agent.
Use tool outputs to answer.
Requirements:
- Ground every key claim in retrieved context.
- If answer is not in context, explicitly say it is not covered.
- Keep response concise and practical.
- For repository-analysis answers, cite file paths/line markers that appear in retrieved context.
- If a generation tool created files, DO NOT print raw JSON in chat. Instead, mention the generated file and what it contains.
- Never claim a file was generated unless tool outputs actually contain a `saved_to` result.
- Keep guidance provider-neutral unless the user explicitly asked for a specific cloud/vendor.
- If multiple cloud-specific options appear in context, summarize the generic/common path first (Prometheus + Grafana), and skip vendor-specific steps by default.
- Include a final line 'Confidence: High|Medium|Low'.
{f"- Improvement hint: {reflection_hint}" if reflection_hint else ""}

Question:
{question}

Tool outputs summary:
{tool_summary}

Retrieved context:
{context}
"""
    return _chat(prompt, temperature=0)


def _remove_unrequested_file_claims(
    answer: str, tool_outputs: list[dict[str, Any]]
) -> str:
    has_artifacts = len(_extract_artifacts(tool_outputs)) > 0
    if has_artifacts:
        return answer
    filtered_lines: list[str] = []
    for line in answer.splitlines():
        lowered = line.lower()
        if ("generated" in lowered and "file" in lowered) or (
            "has been generated" in lowered
        ):
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines).strip()


def _contains_vendor_terms(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in VENDOR_TERMS)


def _should_rewrite_vendor_specific(question: str, answer: str) -> bool:
    # If user did not ask for a vendor but answer mentions one, rewrite to neutral guidance.
    return (not _contains_vendor_terms(question)) and _contains_vendor_terms(answer)


def _enforce_vendor_neutral_answer(question: str, answer: str) -> str:
    if not _should_rewrite_vendor_specific(question, answer):
        return answer
    filtered_lines: list[str] = []
    for line in answer.splitlines():
        if _contains_vendor_terms(line):
            continue
        filtered_lines.append(line)
    if not filtered_lines:
        return answer
    return "\n".join(filtered_lines).strip()


def run_agent(
    question: str,
    allow_actions: bool = True,
    session_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        request_mode = _infer_request_mode(question, session_context)
        allow_generation_actions = allow_actions and request_mode in {
            "generate_dashboard",
            "generate_alert",
            "generate_prompt",
        }
        allow_update_actions = allow_actions and request_mode == "update_dashboard"
        allow_patch_actions = allow_actions and request_mode == "patch_dashboard"
        allowed_tools = _allowed_tools_for_request(request_mode, allow_actions)
        initial_calls = (
            _direct_patch_plan(
                question,
                session_context=session_context,
                allow_actions=allow_patch_actions,
                request_mode=request_mode,
            )
            or _direct_update_plan(
                question,
                session_context=session_context,
                allow_actions=allow_update_actions,
                request_mode=request_mode,
            )
            or _direct_generation_plan(
                question,
                allow_actions=allow_generation_actions,
                request_mode=request_mode,
            )
            or _direct_repo_analysis_plan(
                question,
                request_mode=request_mode,
            )
            or _plan_tools(question, allowed_tools=allowed_tools)
        )
        initial_calls = _sanitize_tool_calls(
            initial_calls, allowed_tools=allowed_tools, question=question
        )
        tool_outputs = _execute_tools(initial_calls)
        self_check_retried = False

        # Safety net: if retrieval was requested but produced no usable context,
        # run a deterministic retrieval call with sanitized arguments.
        context_tools = {"retrieve_context", "retrieve_repo_context", "read_repo_file"}
        requested_retrieval_tool = None
        for call in initial_calls:
            tool_name = str(call.get("tool", ""))
            if tool_name in context_tools:
                requested_retrieval_tool = tool_name
                break
        has_context = _has_retrieved_context(tool_outputs)
        if (
            requested_retrieval_tool in {"retrieve_context", "retrieve_repo_context"}
            and not has_context
        ):
            forced_retrieval_calls = [
                {
                    "tool": requested_retrieval_tool,
                    "arguments": {"query": str(question), "top_k": DEFAULT_TOP_K},
                }
            ]
            forced_outputs = _execute_tools(forced_retrieval_calls)
            tool_outputs.extend(forced_outputs)
            has_context = _has_retrieved_context(tool_outputs)

        tool_outputs, self_check_retried = _run_dashboard_self_check(
            question=question,
            request_mode=request_mode,
            initial_calls=initial_calls,
            tool_outputs=tool_outputs,
        )

        forced_generation_answer = _build_generation_answer(tool_outputs)
        forced_update_answer = _build_dashboard_update_answer(tool_outputs)
        if forced_generation_answer is not None:
            answer = forced_generation_answer
        elif forced_update_answer is not None:
            answer = forced_update_answer
        elif requested_retrieval_tool in context_tools and not has_context:
            answer = _no_context_answer(question)
        else:
            answer = _synthesize_answer(question, tool_outputs)
            answer = _remove_unrequested_file_claims(answer, tool_outputs)

        if _should_rewrite_vendor_specific(question, answer):
            answer = _synthesize_answer(
                question,
                tool_outputs,
                reflection_hint="Remove Azure/AWS/GCP-specific guidance. Keep the answer provider-neutral.",
            )
            answer = _enforce_vendor_neutral_answer(question, answer)
        context = _build_context_from_tools(tool_outputs)
        if requested_retrieval_tool in context_tools and not has_context:
            reflection = {
                "groundedness_score": 1,
                "completeness_score": 2,
                "actionability_score": 3,
                "need_retry": False,
                "improvement_hint": "Refresh docs index or narrow the query.",
            }
        elif forced_generation_answer is not None or forced_update_answer is not None:
            reflection = {
                "groundedness_score": 5,
                "completeness_score": 4,
                "actionability_score": 5,
                "need_retry": False,
                "improvement_hint": "",
            }
        else:
            reflection = _reflect(question, answer, context)

        retried = self_check_retried
        if reflection["need_retry"]:
            retried = True
            retry_tool = (
                "retrieve_repo_context"
                if request_mode == "repo_qa"
                else "retrieve_context"
            )
            retry_calls = [
                {
                    "tool": retry_tool,
                    "arguments": {"query": question, "top_k": DEFAULT_TOP_K + 2},
                }
            ]
            retry_outputs = _execute_tools(retry_calls)
            tool_outputs.extend(retry_outputs)
            answer = _synthesize_answer(
                question, tool_outputs, reflection_hint=reflection["improvement_hint"]
            )
            context = _build_context_from_tools(tool_outputs)
            reflection = _reflect(question, answer, context)

        return {
            "answer": answer,
            "tool_calls": initial_calls,
            "tool_outputs": tool_outputs,
            "reflection": reflection,
            "artifacts": _extract_artifacts(tool_outputs),
            "retried": retried,
        }
    except Exception as exc:
        return {
            "answer": f"Agent execution failed: {exc}",
            "tool_calls": [],
            "tool_outputs": [],
            "reflection": {
                "groundedness_score": 0,
                "completeness_score": 0,
                "actionability_score": 0,
                "need_retry": False,
                "improvement_hint": "Start LM Studio server and load required models.",
            },
            "artifacts": [],
            "retried": False,
        }
