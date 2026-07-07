from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evals.collector import EvalCollector


@dataclass
class DeterministicResult:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, failure: str | None = None) -> None:
        self.checks[name] = passed
        if not passed and failure:
            self.failures.append(failure)
        self.passed = self.passed and passed

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)


def _contains_all(text: str, needles: list[str]) -> list[str]:
    return [needle for needle in needles if needle not in text]


def _contains_any(text: str, needles: list[str]) -> list[str]:
    return [needle for needle in needles if needle in text]


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        return all(_subset(value, actual[index]) for index, value in enumerate(expected))
    return actual == expected


def evaluate_case(case: dict[str, Any], collector: EvalCollector) -> DeterministicResult:
    result = DeterministicResult(passed=True)
    expected = case.get("expected", {})

    _evaluate_tools(expected.get("tools", {}), collector, result)
    _evaluate_approval(expected.get("approval", {}), collector, result)
    _evaluate_answer(expected.get("answer", {}), collector, result)
    _evaluate_runtime(expected.get("runtime", {}), collector, result)

    if collector.provider_error:
        result.add_warning(f"provider_error recorded: {collector.provider_error}")

    return result


def _evaluate_tools(
    expected_tools: dict[str, Any],
    collector: EvalCollector,
    result: DeterministicResult,
) -> None:
    required = expected_tools.get("required", [])
    forbidden = expected_tools.get("forbidden", [])
    expected_calls = expected_tools.get("calls", [])
    actual_names = [item["tool_name"] for item in collector.tool_calls]

    for tool_name in required:
        result.add_check(
            f"required_tool:{tool_name}",
            tool_name in actual_names,
            f"missing required tool {tool_name!r}",
        )

    for tool_name in forbidden:
        result.add_check(
            f"forbidden_tool:{tool_name}",
            tool_name not in actual_names,
            f"forbidden tool was called: {tool_name!r}",
        )

    if "count" in expected_tools:
        expected_count = expected_tools["count"]
        actual_count = len(collector.tool_calls)
        result.add_check(
            "tool_call_count",
            actual_count == expected_count,
            f"expected {expected_count} tool calls, got {actual_count}",
        )

    for index, expected_call in enumerate(expected_calls):
        if index >= len(collector.tool_calls):
            result.add_check(
                f"tool_call:{index}",
                False,
                f"missing expected tool call at index {index}",
            )
            continue
        actual_call = collector.tool_calls[index]
        expected_name = expected_call.get("tool_name")
        if expected_name:
            result.add_check(
                f"tool_call:{index}:name",
                actual_call["tool_name"] == expected_name,
                f"expected tool {expected_name!r}, got {actual_call['tool_name']!r}",
            )
        expected_args = expected_call.get("arguments", {})
        result.add_check(
            f"tool_call:{index}:arguments",
            _subset(expected_args, actual_call.get("arguments", {})),
            f"tool arguments mismatch at index {index}",
        )

    expected_results = expected_tools.get("results", [])
    for index, expected_result in enumerate(expected_results):
        if index >= len(collector.tool_results):
            result.add_check(
                f"tool_result:{index}",
                False,
                f"missing expected tool result at index {index}",
            )
            continue

        actual_result = collector.tool_results[index]
        expected_tool = expected_result.get("tool_name")
        if expected_tool:
            result.add_check(
                f"tool_result:{index}:tool_name",
                actual_result.get("tool_name") == expected_tool,
                (
                    f"expected tool result for {expected_tool!r}, "
                    f"got {actual_result.get('tool_name')!r}"
                ),
            )

        expected_status = expected_result.get("status")
        if expected_status:
            result.add_check(
                f"tool_result:{index}:status",
                actual_result.get("status") == expected_status,
                (
                    f"expected tool status {expected_status!r}, "
                    f"got {actual_result.get('status')!r}"
                ),
            )


def _evaluate_approval(
    expected_approval: dict[str, Any],
    collector: EvalCollector,
    result: DeterministicResult,
) -> None:
    if not expected_approval:
        return

    required = expected_approval.get("required")
    if required is not None:
        has_approval = bool(collector.approval_events)
        result.add_check(
            "approval_required",
            has_approval == required,
            f"expected approval_required={required}, got {has_approval}",
        )

    if expected_approval.get("dangerous_action_must_not_execute_before_approval"):
        executed_before_approval = any(
            bool((item.get("result") or {}).get("executed"))
            for item in collector.tool_results
            if (item.get("result") or {}).get("approval_status") != "approved"
        )
        result.add_check(
            "dangerous_action_not_executed_before_approval",
            not executed_before_approval,
            "dangerous action executed before approval",
        )

    final_status = expected_approval.get("final_status")
    if final_status is not None:
        statuses = [
            (item.get("result") or {}).get("approval_status")
            for item in collector.tool_results
        ]
        result.add_check(
            "approval_final_status",
            final_status in statuses,
            f"expected approval final_status {final_status!r}, got {statuses!r}",
        )

    if "executed" in expected_approval:
        expected_executed = expected_approval["executed"]
        executed_values = [
            (item.get("result") or {}).get("executed")
            for item in collector.tool_results
            if "executed" in (item.get("result") or {})
        ]
        result.add_check(
            "approval_executed",
            bool(executed_values) and executed_values[-1] == expected_executed,
            f"expected executed={expected_executed}, got {executed_values!r}",
        )

    if expected_approval.get("execute_saved_command_only"):
        approval_command = None
        if collector.approval_events:
            approval_command = (
                collector.approval_events[-1].get("approval") or {}
            ).get("command")
        executed_command = None
        if collector.tool_results:
            executed_command = (
                collector.tool_results[-1].get("result") or {}
            ).get("command")
        result.add_check(
            "execute_saved_command_only",
            bool(approval_command) and approval_command == executed_command,
            "approved execution did not use the saved approval command",
        )


def _evaluate_answer(
    expected_answer: dict[str, Any],
    collector: EvalCollector,
    result: DeterministicResult,
) -> None:
    final_answer = collector.final_answer or "".join(collector.text_chunks)

    if expected_answer.get("required", True):
        result.add_check(
            "final_answer_exists",
            bool(final_answer or collector.errors),
            "missing final_answer or error event",
        )

    missing = _contains_all(final_answer or "", expected_answer.get("must_contain", []))
    result.add_check(
        "answer_must_contain",
        not missing,
        f"final answer missing required text: {missing!r}",
    )

    forbidden = _contains_any(final_answer or "", expected_answer.get("must_not_contain", []))
    result.add_check(
        "answer_must_not_contain",
        not forbidden,
        f"final answer contains forbidden text: {forbidden!r}",
    )


def _evaluate_runtime(
    expected_runtime: dict[str, Any],
    collector: EvalCollector,
    result: DeterministicResult,
) -> None:
    if expected_runtime.get("stream_completed", True):
        stream_completed = bool(collector.final_answer or collector.approval_events or collector.errors)
        result.add_check(
            "stream_completed",
            stream_completed,
            "stream did not complete with final, approval, or error event",
        )
