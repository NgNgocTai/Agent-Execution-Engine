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
from services.agent_service import AgentEngine
from services.skills.alarm_statistics import AlarmStatisticsSkill
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill
from services.skills.noc_alarm_enrichment import NocAlarmEnrichmentSkill
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor

CASES_DIR = Path(__file__).resolve().parent / "cases"


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
        **kwargs: Any,
    ) -> SkillResult:
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


async def run_cases(kind: str) -> int:
    files = []
    if kind in ("all", "skills"):
        files.append(("skill", load_cases("skill_cases.json")))
    if kind in ("all", "agent"):
        files.append(("agent", load_cases("agent_cases.json")))

    passed = 0
    failed = 0
    for case_type, cases in files:
        for case in cases:
            label = f"{case_type}: {case['name']}"
            try:
                if case_type == "skill":
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
    parser = argparse.ArgumentParser(description="Run deterministic NOC Agent eval cases.")
    parser.add_argument(
        "--kind",
        choices=("all", "agent", "skills"),
        default="all",
        help="Eval suite to run.",
    )
    args = parser.parse_args()
    return asyncio.run(run_cases(args.kind))


if __name__ == "__main__":
    raise SystemExit(main())
