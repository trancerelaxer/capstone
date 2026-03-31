import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import lint_grafana_dashboard, run_agent

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "data" / "output"

BENCHMARK_CASES: list[dict[str, Any]] = [
    {
        "id": "entrypoint_modes",
        "question": "From src/main.py, list the exact mode values supported by the router.",
        "required_terms": ["web", "cli", "ingest-docs"],
    },
    {
        "id": "web_api_endpoint",
        "question": "From src/web_ui.py, what exact POST endpoint path accepts user questions?",
        "required_terms": ["/api/ask"],
    },
    {
        "id": "artifact_tools",
        "question": "From src/agent.py, name exactly two artifact-generation tool names.",
        "required_terms": ["generate_grafana_dashboard", "generate_grafana_alert_rule"],
    },
    {
        "id": "docs_root_constant",
        "question": "From src/grafana_docs_ingest.py, what is the exact DOCS_ROOT URL constant?",
        "required_terms": ["https://grafana.com/docs/grafana/latest/"],
    },
    {
        "id": "reflection_scores",
        "question": "From src/agent.py reflection JSON, what are the three score field names?",
        "required_terms": [
            "groundedness_score",
            "completeness_score",
            "actionability_score",
        ],
    },
]

DASHBOARD_GOLDEN_CASES: list[dict[str, Any]] = [
    {
        "id": "k8s_pod_dashboard",
        "question": (
            "Create a production-ready Grafana dashboard JSON for Kubernetes pod monitoring. "
            'Use Prometheus datasource uid "default". '
            "No templating variables. "
            "Panels required: Pod CPU rate 5m, Pod Memory working set MiB, Pod Restarts increase 1h, "
            "Running/Pending/Failed pods by namespace."
        ),
        "profile": "kubernetes_pods",
        "min_panel_count": 6,
    },
    {
        "id": "api_latency_error_dashboard",
        "question": (
            "Create a production-ready Grafana dashboard JSON for API monitoring with Prometheus uid default. "
            "Include latency and error-rate panels with valid PromQL."
        ),
        "profile": "generic",
        "min_panel_count": 2,
    },
]


def _contains_all_terms(answer: str, terms: list[str]) -> tuple[bool, list[str]]:
    lowered = answer.lower()
    missing = [term for term in terms if term.lower() not in lowered]
    return (len(missing) == 0, missing)


def run_benchmark(allow_actions: bool = False) -> dict[str, Any]:
    case_results: list[dict[str, Any]] = []
    passes = 0
    total_groundedness = 0
    total_completeness = 0
    total_actionability = 0

    for case in BENCHMARK_CASES:
        result = run_agent(
            case["question"], allow_actions=allow_actions, session_context={}
        )
        answer = str(result.get("answer", ""))
        reflection = result.get("reflection", {})
        passed, missing_terms = _contains_all_terms(answer, case["required_terms"])
        if passed:
            passes += 1

        groundedness = int(reflection.get("groundedness_score", 0) or 0)
        completeness = int(reflection.get("completeness_score", 0) or 0)
        actionability = int(reflection.get("actionability_score", 0) or 0)
        total_groundedness += groundedness
        total_completeness += completeness
        total_actionability += actionability

        case_results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "required_terms": case["required_terms"],
                "passed": passed,
                "missing_terms": missing_terms,
                "answer": answer,
                "reflection": reflection,
            }
        )

    total = len(BENCHMARK_CASES)
    denominator = max(1, total)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_cases": total,
        "passed_cases": passes,
        "pass_rate": round(passes / denominator, 3),
        "average_reflection_scores": {
            "groundedness_score": round(total_groundedness / denominator, 2),
            "completeness_score": round(total_completeness / denominator, 2),
            "actionability_score": round(total_actionability / denominator, 2),
        },
        "cases": case_results,
    }
    return report


def _latest_artifact_name(result: dict[str, Any], artifact_type: str) -> str:
    artifacts = result.get("artifacts", [])
    if not isinstance(artifacts, list):
        return ""
    for item in reversed(artifacts):
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).strip() != artifact_type:
            continue
        name = str(item.get("name", "")).strip()
        if name:
            return os.path.basename(name)
    return ""


def _templating_is_empty(dashboard_file: str) -> bool:
    path = OUTPUT_DIR / os.path.basename(dashboard_file)
    if not path.exists() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    dashboard = payload.get("dashboard", {})
    if not isinstance(dashboard, dict):
        return False
    templating = dashboard.get("templating", {})
    if not isinstance(templating, dict):
        return False
    templating_list = templating.get("list", [])
    return isinstance(templating_list, list) and len(templating_list) == 0


def run_dashboard_golden_suite() -> dict[str, Any]:
    case_results: list[dict[str, Any]] = []
    total_score = 0.0
    passed_cases = 0

    for case in DASHBOARD_GOLDEN_CASES:
        result = run_agent(case["question"], allow_actions=True, session_context={})
        dashboard_file = _latest_artifact_name(result, "dashboard_json")

        lint_result: dict[str, Any] = {
            "ok": False,
            "issues": ["No dashboard artifact generated."],
            "warnings": [],
            "panel_count": 0,
        }
        if dashboard_file:
            try:
                lint_result = lint_grafana_dashboard(
                    filename=dashboard_file,
                    auto_fix=False,
                    enforce_no_templating=True,
                    required_datasource_uid="default",
                    profile=str(case.get("profile", "generic")),
                    check_metric_availability=False,
                )
            except Exception as exc:
                lint_result = {
                    "ok": False,
                    "issues": [str(exc)],
                    "warnings": [],
                    "panel_count": 0,
                }

        panel_count = int(lint_result.get("panel_count", 0) or 0)
        min_panel_count = int(case.get("min_panel_count", 0) or 0)
        lint_ok = bool(lint_result.get("ok", False))
        templating_empty = (
            _templating_is_empty(dashboard_file) if dashboard_file else False
        )
        artifact_generated = bool(dashboard_file)
        panel_count_ok = panel_count >= min_panel_count

        score = 0.0
        if artifact_generated:
            score += 30.0
        if lint_ok:
            score += 35.0
        if panel_count_ok:
            score += 20.0
        if templating_empty:
            score += 15.0
        score = round(score, 1)
        total_score += score

        passed = score >= 80.0
        if passed:
            passed_cases += 1

        case_results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "dashboard_file": dashboard_file,
                "profile": case.get("profile", "generic"),
                "checks": {
                    "artifact_generated": artifact_generated,
                    "lint_ok": lint_ok,
                    "panel_count": panel_count,
                    "min_panel_count": min_panel_count,
                    "panel_count_ok": panel_count_ok,
                    "templating_empty": templating_empty,
                },
                "score": score,
                "passed": passed,
                "lint": {
                    "issues": lint_result.get("issues", []),
                    "warnings": lint_result.get("warnings", []),
                },
                "answer": str(result.get("answer", "")),
                "reflection": result.get("reflection", {}),
            }
        )

    total = len(DASHBOARD_GOLDEN_CASES)
    denominator = max(1, total)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "suite": "dashboard_golden",
        "total_cases": total,
        "passed_cases": passed_cases,
        "pass_rate": round(passed_cases / denominator, 3),
        "average_score": round(total_score / denominator, 2),
        "cases": case_results,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate agent behavior on fixed benchmark prompts."
    )
    parser.add_argument(
        "--allow-actions",
        action="store_true",
        help="Allow action tools during evaluation (default false for safer deterministic checks).",
    )
    parser.add_argument(
        "--suite",
        choices=["default", "dashboard", "all"],
        default="default",
        help="Evaluation suite to run.",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.suite in {"default", "all"}:
        report = run_benchmark(allow_actions=args.allow_actions)
        report_path = OUTPUT_DIR / "agent_evaluation_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2))
        print("Default evaluation complete.")
        print(
            f"Pass rate: {report['passed_cases']}/{report['total_cases']} ({report['pass_rate'] * 100:.1f}%)"
        )
        print(
            "Average reflection scores: "
            f"groundedness={report['average_reflection_scores']['groundedness_score']}, "
            f"completeness={report['average_reflection_scores']['completeness_score']}, "
            f"actionability={report['average_reflection_scores']['actionability_score']}"
        )
        print(f"Saved report: {report_path}")

    if args.suite in {"dashboard", "all"}:
        dashboard_report = run_dashboard_golden_suite()
        dashboard_report_path = OUTPUT_DIR / "dashboard_golden_report.json"
        dashboard_report_path.write_text(
            json.dumps(dashboard_report, ensure_ascii=True, indent=2)
        )
        print("Dashboard golden suite complete.")
        print(
            f"Pass rate: {dashboard_report['passed_cases']}/{dashboard_report['total_cases']} "
            f"({dashboard_report['pass_rate'] * 100:.1f}%)"
        )
        print(f"Average score: {dashboard_report['average_score']}/100")
        print(f"Saved report: {dashboard_report_path}")


if __name__ == "__main__":
    main()
