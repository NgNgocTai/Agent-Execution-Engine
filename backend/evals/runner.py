from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from core.schemas import AgentEventType, NormalizedResponse, NormalizedToolCall
from evals.collector import EvalCollector
from evals.deterministic import evaluate_case
from evals.ragas_evaluator import evaluate_quality
from evals.report import print_summary, write_reports
from services.agent_service import AgentEngine
from services.skills.alarm_statistics import AlarmStatisticsSkill
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill
from services.skills.noc_alarm_enrichment import NocAlarmEnrichmentSkill
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor

CASES_DIR = Path(__file__).resolve().parent / "cases"
DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


class EvalFailure(AssertionError):
    pass


class FakeAlarmRepo:
    def count_alarms(self, where_clause: str, params: dict) -> int:
        if "status = 'active'" in where_clause:
            return 3
        return 10

    def group_alarms(
        self,
        group_col: str,
        where_clause: str,
        params: dict,
        limit: int = 0,
    ) -> list[dict]:
        if group_col == "severity":
            return [{"key": "Critical", "count": 6}, {"key": "Major", "count": 4}]
        if group_col == "error_code":
            return [{"key": "ERR-001", "count": 5}]
        if group_col == "device_id":
            return [{"key": "DEV-001", "count": 4}, {"key": "DEV-002", "count": 2}]
        return []

    def get_active_alarms_by_device(self, device_id: str, limit: int = 20) -> list[dict]:
        if device_id == "DEV-001":
            return [
                {
                    "alarm_id": "A-1",
                    "error_code": "ERR-001",
                    "severity": "Critical",
                    "time_created": "2026-06-28 08:00:00",
                    "status": "active",
                }
            ]
        return []

    def get_alarms(self, error_code: str, window_min: int, limit: int = 50):
        if error_code != "ERR-001":
            return [], 0
        alarms = [
            {
                "alarm_id": "ALM-1",
                "error_code": error_code,
                "device_id": "DEV-001",
                "time_created": "2026-06-28 08:00:00",
                "time_solved": None,
                "status": "active",
                "severity": "Critical",
                "raw_log": (
                    "[2026-06-28 08:00:00] Encoder-CT-Station-007-004385 "
                    "Signal Loss sev=Critical val=8 thr=68 st=ACK"
                ),
                "description": "Phoi hop voi vendor de xu ly",
            }
        ]
        return alarms, len(alarms)


class FakeDeviceRepo:
    async def get_errors_by_domain(self, domain: str) -> list[dict]:
        if domain.lower() in ("transport", "ran", "power", "core"):
            return [{"error_code": "ERR-001"}]
        return []

    async def get_error_by_code(self, error_code: str) -> dict | None:
        if error_code == "ERR-001":
            return {"error_code": error_code, "name": "Signal Loss"}
        return None

    async def get_error_by_name(self, name: str) -> dict | None:
        if name.upper() in ("SIGNAL LOSS", "LINK DOWN", "LINK_DOWN"):
            return {
                "error_code": "ERR-001",
                "name": "Signal Loss",
                "description": "Link/interface signal loss",
                "domain": "Transport",
                "default_severity": "Critical",
            }
        return None

    async def get_device_with_details(
        self,
        device_id: str | None = None,
        device_name: str | None = None,
    ) -> dict | None:
        if device_id in ("DEV-001", None) and device_name in ("NOC-APP-01", None):
            if device_id == "DEV-001" or device_name == "NOC-APP-01":
                return self._device()
        return None

    async def get_devices_by_names(self, names: list[str]) -> list[dict]:
        known = {"10.0.0.1", "NOC-APP-01", "Encoder-CT-Station-007-004385"}
        if any(name in known for name in names):
            return [self._device(name="Encoder-CT-Station-007-004385")]
        return []

    @staticmethod
    def _device(name: str = "NOC-APP-01") -> dict:
        return {
            "device_id": "DEV-001",
            "name": name,
            "device_type": "Server",
            "ip_address": "10.0.0.1",
            "vendor_name": "Linux",
            "station_name": "NOC",
        }


class FakeNodeHealthSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "node_health_remediate"

    @property
    def description(self) -> str:
        return "Fake node health skill for deterministic evals."

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["host"],
        }

    async def execute(
        self,
        host: str,
        dry_run: bool = True,
        service: str = "",
        force_action: str | None = None,
        **kwargs: Any,
    ) -> SkillResult:
        if force_action == "restart_service" and not service:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error="Action 'restart_service' requires 'service' parameter.",
            )

        if force_action in {"restart_docker", "restart_service"} and not dry_run:
            command = (
                "sudo -n systemctl restart docker"
                if force_action == "restart_docker"
                else f"docker restart {service or 'alert-engine'}"
            )
            approval_metadata = {
                "host": host,
                "service": service,
                "action": force_action,
                "command": command,
                "metrics": {"mem_pct": 91.0, "cpu_pct": 85.0, "disk_pct": 22.0},
                "thresholds": {"ram_threshold": 80, "cpu_threshold": 80},
                "evaluation": {"ram_high": True, "cpu_high": True},
                "reason": "Dangerous remediation requires human approval before execution.",
            }
            return SkillResult(
                status=SkillStatus.SUCCESS,
                data={
                    **approval_metadata,
                    "dry_run": dry_run,
                    "executed": False,
                    "approval_status": "pending",
                },
                needs_approval=True,
                approval_metadata=approval_metadata,
            )

        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={
                "host": host,
                "metrics": {"mem_pct": 41.2, "cpu_pct": 18.5, "disk_pct": 22.0},
                "thresholds": {"ram_threshold": 80, "cpu_threshold": 80},
                "evaluation": {"ram_high": False, "cpu_high": False},
                "action": "none",
                "command": None,
                "dry_run": dry_run,
                "executed": False,
            },
        )


class FakeApprovalRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_pending(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return kwargs


class ScriptedProvider:
    model_name = "eval-scripted"

    def __init__(self, tool_calls: list[dict], final_text: str) -> None:
        self.tool_calls = tool_calls
        self.final_text = final_text
        self.call_count = 0

    async def chat_completion(self, messages, tools, stream=True):
        self.call_count += 1
        if self.call_count == 1 and self.tool_calls:
            available_tools = {tool.name for tool in tools}
            for call in self.tool_calls:
                if call["tool_name"] not in available_tools:
                    raise EvalFailure(f"Tool schema not available: {call['tool_name']}")
            yield NormalizedResponse(thinking_content="Evaluating requested tool call.")
            yield NormalizedResponse(
                tool_calls=[
                    NormalizedToolCall(
                        call_id=call["call_id"],
                        tool_name=call["tool_name"],
                        arguments=call.get("arguments", {}),
                    )
                    for call in self.tool_calls
                ],
                stop_reason="tool_calls",
            )
            return

        yield NormalizedResponse(text_content=self.final_text, stop_reason="end_turn")


def load_cases(filename: str) -> list[dict]:
    with (CASES_DIR / filename).open(encoding="utf-8") as f:
        return json.load(f)


def load_dataset(filename: str) -> list[dict]:
    with (DATASETS_DIR / filename).open(encoding="utf-8") as f:
        return json.load(f)


def load_quality_references(
    filename: str = "noc_agent_ragas_references.json",
) -> dict[str, str]:
    path = DATASETS_DIR / filename
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        records = json.load(f)
    return {
        record["case_id"]: record["reference_answer"]
        for record in records
        if record.get("case_id") and record.get("reference_answer")
    }


def apply_quality_references(cases: list[dict]) -> int:
    references = load_quality_references()
    applied = 0
    for case in cases:
        reference_answer = references.get(case.get("id"))
        if not reference_answer:
            continue
        case.setdefault("expected", {}).setdefault("answer", {})[
            "reference_answer"
        ] = reference_answer
        applied += 1
    return applied


def build_registry() -> SkillRegistry:
    alarm_repo = FakeAlarmRepo()
    device_repo = FakeDeviceRepo()
    registry = SkillRegistry()
    registry.register(NocAlarmEnrichmentSkill(alarm_repo, device_repo))
    registry.register(AlarmStatisticsSkill(alarm_repo, device_repo))
    registry.register(DeviceInventoryLookupSkill(alarm_repo, device_repo))
    registry.register(FakeNodeHealthSkill())
    return registry


def assert_subset(expected: Any, actual: Any, path: str = "$") -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise EvalFailure(f"{path}: expected dict, got {type(actual).__name__}")
        for key, value in expected.items():
            if key not in actual:
                raise EvalFailure(f"{path}: missing key {key!r}")
            assert_subset(value, actual[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list):
            raise EvalFailure(f"{path}: expected list, got {type(actual).__name__}")
        for index, value in enumerate(expected):
            if index >= len(actual):
                raise EvalFailure(f"{path}: missing list item {index}")
            assert_subset(value, actual[index], f"{path}[{index}]")
        return
    if actual != expected:
        raise EvalFailure(f"{path}: expected {expected!r}, got {actual!r}")


def assert_contains_all(text: str, needles: list[str], context: str) -> None:
    for needle in needles:
        if needle not in text:
            raise EvalFailure(f"{context}: missing {needle!r} in {text!r}")


async def run_skill_case(case: dict) -> None:
    registry = build_registry()
    executor = ToolExecutor(registry)
    result = await executor.execute(case["tool_name"], case.get("arguments", {}))

    expected_status = case["expected_status"]
    if result.status != expected_status:
        raise EvalFailure(
            f"expected status {expected_status!r}, got {result.status!r}: {result.error}"
        )

    if "expected_error_contains" in case:
        assert_contains_all(result.error or "", [case["expected_error_contains"]], "error")

    if "expected_data_keys" in case:
        if not isinstance(result.data, dict):
            raise EvalFailure("expected result.data to be dict")
        for key in case["expected_data_keys"]:
            if key not in result.data:
                raise EvalFailure(f"missing data key {key!r}")

    if "expected_data" in case:
        assert_subset(case["expected_data"], result.data, "$.data")


async def run_agent_case(case: dict) -> None:
    registry = build_registry()
    executor = ToolExecutor(registry)
    engine = AgentEngine(executor, registry, tracer=None)
    provider = ScriptedProvider(
        tool_calls=case.get("provider_tool_calls", []),
        final_text=case["final_text"],
    )

    events = [
        event
        async for event in engine.run(
            session_id=f"eval-{case['name']}",
            user_message=case["user_message"],
            provider=provider,
            history=[],
        )
    ]

    tool_starts = [event for event in events if event.type == AgentEventType.TOOL_CALL_START]
    tool_results = [event for event in events if event.type == AgentEventType.TOOL_CALL_RESULT]
    finals = [event for event in events if event.type == AgentEventType.FINAL_ANSWER]

    expected_calls = case.get("expected_tool_calls", [])
    if len(tool_starts) != len(expected_calls):
        raise EvalFailure(
            f"expected {len(expected_calls)} tool starts, got {len(tool_starts)}"
        )

    for expected, event in zip(expected_calls, tool_starts):
        data = event.data
        if data["tool_name"] != expected["tool_name"]:
            raise EvalFailure(
                f"expected tool {expected['tool_name']!r}, got {data['tool_name']!r}"
            )
        assert_subset(expected.get("arguments", {}), data.get("arguments", {}), "$.arguments")

    expected_statuses = case.get("expected_tool_statuses", [])
    actual_statuses = [event.data["status"] for event in tool_results]
    if expected_statuses and actual_statuses != expected_statuses:
        raise EvalFailure(f"expected statuses {expected_statuses!r}, got {actual_statuses!r}")

    if not finals:
        raise EvalFailure("missing final_answer event")
    final_message = finals[-1].data["updated_messages"][-1]
    final_text = final_message.content or ""
    assert_contains_all(final_text, case.get("expected_final_contains", []), "final answer")


async def run_benchmark_case(
    case: dict,
    quality_evaluator: str = "none",
) -> dict[str, Any]:
    registry = build_registry()
    executor = ToolExecutor(registry)
    engine = AgentEngine(executor, registry, tracer=None)
    provider = ScriptedProvider(
        tool_calls=case.get("provider_tool_calls", []),
        final_text=case.get("final_text", ""),
    )

    collector = EvalCollector()
    approval_repo = FakeApprovalRepo()
    async for event in engine.run(
        session_id=f"eval-{case['id']}",
        user_message=case["user_input"],
        provider=provider,
        history=[],
        approval_repo=approval_repo,
    ):
        collector.record(event)

    if case.get("type") == "approval_flow":
        _simulate_approval_decision(case, collector, approval_repo)

    deterministic = evaluate_case(case, collector)
    llm_quality = await evaluate_quality(case, collector, evaluator=quality_evaluator)

    return {
        "case_id": case["id"],
        "name": case.get("name", case["id"]),
        "type": case.get("type", "single_turn"),
        "passed": deterministic.passed,
        "checks": deterministic.checks,
        "failures": deterministic.failures,
        "warnings": deterministic.warnings,
        "llm_quality": llm_quality,
        "latency_ms": collector.latency_ms,
        "tool_calls": collector.tool_calls,
        "tool_results": collector.tool_results,
        "approval_events": collector.approval_events,
        "provider_error": collector.provider_error,
        "final_answer": collector.final_answer,
    }


async def run_quality_case(
    case: dict[str, Any],
    quality_evaluator: str = "ragas",
) -> dict[str, Any]:
    collector = collector_from_frozen_case(case)
    failures: list[str] = []
    warnings: list[str] = []

    if not collector.final_answer:
        failures.append("missing_final_answer")
    if not collector.tool_results:
        failures.append("missing_tool_observations")

    llm_quality = await evaluate_quality(
        case,
        collector,
        evaluator=quality_evaluator,
    )
    warnings.extend(llm_quality.get("warnings", []))

    quality_scores = [
        llm_quality.get("groundedness_score"),
        llm_quality.get("correctness_score"),
    ]
    if quality_evaluator == "ragas" and not any(score is not None for score in quality_scores):
        warnings.append("missing_ragas_quality_scores")

    return {
        "case_id": case["id"],
        "name": case.get("name", case["id"]),
        "type": case.get("type", "ragas_quality"),
        "provider": case.get("provider") or case.get("source", "frozen_observation"),
        "model": case.get("model"),
        "comparison_group": case.get("comparison_group"),
        "passed": not failures,
        "checks": {
            "final_answer_exists": bool(collector.final_answer),
            "tool_observations_exist": bool(collector.tool_results),
            "ragas_quality_scores_exist": any(score is not None for score in quality_scores),
        },
        "failures": failures,
        "warnings": warnings,
        "llm_quality": llm_quality,
        "latency_ms": case.get("latency_ms"),
        "tool_calls": collector.tool_calls,
        "tool_results": collector.tool_results,
        "approval_events": collector.approval_events,
        "provider_error": None,
        "final_answer": collector.final_answer,
    }


def collector_from_frozen_case(case: dict[str, Any]) -> EvalCollector:
    collector = EvalCollector()
    collector.final_answer = case.get("final_answer")
    if collector.final_answer:
        collector.text_chunks.append(collector.final_answer)
    collector.latency_ms = case.get("latency_ms")

    for index, item in enumerate(case.get("tool_results", [])):
        tool_name = item.get("tool_name")
        collector.tool_calls.append(
            {
                "call_id": item.get("call_id", f"frozen-{index + 1}"),
                "tool_name": tool_name,
                "arguments": item.get("arguments", {}),
                "iteration": item.get("iteration", 1),
            }
        )
        collector.tool_results.append(
            {
                "call_id": item.get("call_id", f"frozen-{index + 1}"),
                "tool_name": tool_name,
                "status": item.get("status", "success"),
                "result": item.get("data", item.get("result")),
                "error": item.get("error"),
                "execution_time_ms": item.get("execution_time_ms", 0.0),
                "iteration": item.get("iteration", 1),
            }
        )
    return collector


def _simulate_approval_decision(
    case: dict[str, Any],
    collector: EvalCollector,
    approval_repo: FakeApprovalRepo,
) -> None:
    if not collector.approval_events:
        return

    event = collector.approval_events[-1]
    approval = event["approval"]
    command = approval.get("command")
    host = approval.get("host")
    decision = case.get("decision", "reject")

    if decision == "approve":
        tool_result = {
            **approval,
            "approval_status": "approved",
            "executed": True,
            "command_succeeded": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "host": host,
            "command": command,
        }
        collector.record_approval_decision(
            call_id=event["call_id"],
            tool_name=event["tool_name"],
            status=SkillStatus.SUCCESS,
            result=tool_result,
            summary=f"approved and executed saved command {command} on {host}",
        )
        return

    tool_result = {
        **approval,
        "approval_status": "rejected",
        "executed": False,
        "command_succeeded": False,
        "exit_code": None,
        "stdout": None,
        "stderr": "User rejected the requested remediation.",
        "host": host,
        "command": command,
    }
    collector.record_approval_decision(
        call_id=event["call_id"],
        tool_name=event["tool_name"],
        status=SkillStatus.BUSINESS_ERROR,
        result=tool_result,
        summary=f"rejected remediation request; command {command} was not executed",
    )


async def run_benchmark(
    dataset: str,
    results_dir: Path,
    quality_evaluator: str = "none",
) -> int:
    cases = load_dataset(dataset)
    records = []
    failed = 0

    for case in cases:
        record = await run_benchmark_case(case, quality_evaluator=quality_evaluator)
        records.append(record)
        if record["passed"]:
            print(f"PASS e2e-workflow: {case['id']} {case.get('name', '')}")
        else:
            failed += 1
            print(f"FAIL e2e-workflow: {case['id']} {case.get('name', '')}")
            for failure in record["failures"]:
                print(f"  {failure}")

    write_reports(records, results_dir)
    print_summary(records)
    print(f"\nReports written to: {results_dir}")
    return failed


async def run_quality(
    dataset: str,
    results_dir: Path,
    quality_evaluator: str = "ragas",
    source_filter: str | None = None,
    scope_filter: str | None = None,
) -> int:
    cases = load_dataset(dataset)
    applied_references = apply_quality_references(cases)
    if applied_references:
        print(f"Human-labeled references applied: {applied_references}")
    if source_filter:
        before_count = len(cases)
        cases = [case for case in cases if case.get("source") == source_filter]
        print(f"Quality source filter: {source_filter} ({len(cases)}/{before_count} cases)")
    if scope_filter:
        before_count = len(cases)
        cases = [case for case in cases if case.get("quality_scope") == scope_filter]
        print(f"Quality scope filter: {scope_filter} ({len(cases)}/{before_count} cases)")

    records = []
    failed = 0

    for case in cases:
        record = await run_quality_case(case, quality_evaluator=quality_evaluator)
        records.append(record)
        if record["passed"]:
            print(f"PASS ragas-quality: {case['id']} {case.get('name', '')}")
        else:
            failed += 1
            print(f"FAIL ragas-quality: {case['id']} {case.get('name', '')}")
            for failure in record["failures"]:
                print(f"  {failure}")

    write_reports(records, results_dir)
    print_summary(records)
    print(f"\nReports written to: {results_dir}")
    return failed


async def run_cases(kind: str) -> int:
    files = []
    if kind in ("all", "contracts", "skills"):
        files.append(("contract-skill", load_cases("skill_cases.json")))
    if kind in ("all", "contracts", "agent"):
        files.append(("contract-agent-tool", load_cases("agent_cases.json")))

    passed = 0
    failed = 0
    for case_type, cases in files:
        for case in cases:
            label = f"{case_type}: {case['name']}"
            try:
                if case_type == "contract-skill":
                    await run_skill_case(case)
                else:
                    await run_agent_case(case)
            except Exception as exc:
                failed += 1
                print(f"FAIL {label}")
                print(f"  {exc}")
            else:
                passed += 1
                print(f"PASS {label}")

    print(f"\nSummary: {passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run NOC Agent tests and workflow evals.")
    parser.add_argument(
        "--kind",
        choices=("all", "contracts", "agent", "skills", "benchmark", "quality"),
        default="all",
        help="Eval suite to run.",
    )
    parser.add_argument(
        "--dataset",
        default="noc_agent_eval_cases.json",
        help="End-to-End Workflow Eval dataset file under backend/evals/datasets.",
    )
    parser.add_argument(
        "--results-dir",
        default=str(RESULTS_DIR),
        help="Directory for JSON/CSV benchmark reports.",
    )
    parser.add_argument(
        "--quality-evaluator",
        choices=("none", "ragas"),
        default="none",
        help=(
            "Answer-quality evaluator for benchmark reports. "
            "Default 'none' runs deterministic checks only; use 'ragas' "
            "to call optional LLM-as-a-Judge metrics."
        ),
    )
    parser.add_argument(
        "--quality-source",
        default=None,
        help=(
            "Optional source filter for --kind quality, for example "
            "'real_conversation_snapshot' to exclude sample frozen observations."
        ),
    )
    parser.add_argument(
        "--quality-scope",
        default=None,
        help=(
            "Optional quality-scope filter for --kind quality, for example "
            "'answer_quality' to exclude no-data, approval, and safety cases "
            "from Ragas quality averages."
        ),
    )
    args = parser.parse_args()

    async def _run() -> int:
        exit_code = 0
        if args.kind in ("all", "contracts", "agent", "skills"):
            exit_code = await run_cases(args.kind)
        if args.kind in ("all", "benchmark"):
            benchmark_failed = await run_benchmark(
                args.dataset,
                Path(args.results_dir),
                quality_evaluator=args.quality_evaluator,
            )
            if benchmark_failed:
                exit_code = 1
        if args.kind == "quality":
            quality_evaluator = args.quality_evaluator
            if quality_evaluator == "none":
                quality_evaluator = "ragas"
            quality_failed = await run_quality(
                args.dataset,
                Path(args.results_dir),
                quality_evaluator=quality_evaluator,
                source_filter=args.quality_source,
                scope_filter=args.quality_scope,
            )
            if quality_failed:
                exit_code = 1
        return exit_code

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
