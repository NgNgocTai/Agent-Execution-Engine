from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings
from core.schemas import AgentEventType, NormalizedMessage
from services.agent_service import AgentEngine
from services.evidence import evidence, observation
from services.grounding import check_answer
from services.skills.base import SkillResult, SkillStatus
from services.skills.registry import SkillRegistry
from test_skill_guides import FakeProvider

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
EV = "ev_" + "a" * 20
OTHER = "ev_" + "b" * 20


class Skill:
    name = "alarm_statistics"


class ObservationTest(unittest.TestCase):
    def test_observed_at_is_the_newest_record_event_time_not_retrieval_time(self):
        data = {"alarms": [
            {"alarm_id": 1, "time_created": "2026-11-17 11:50:00"},
            {"alarm_id": 2, "time_created": "2026-11-17T11:58:30+00:00"},
            {"alarm_id": 3, "time_created": "garbage"},
        ]}
        facts = observation(data, NOW)
        self.assertEqual(facts["observed_at"], "2026-11-17T11:58:30+00:00")
        self.assertEqual(facts["oldest_event_at"], "2026-11-17T11:50:00+00:00")
        self.assertEqual(facts["observed_basis"], "record_event_time")
        self.assertEqual(facts["freshness"], "unknown")
        self.assertEqual((facts["record_count"], "alarm_id" in facts["fields"]), (3, True))
        self.assertTrue(any("does not prove the source is current" in w for w in facts["warnings"]))

    def test_no_time_in_the_result_leaves_observed_at_unknown(self):
        facts = observation({"devices": [{"device_id": "d1"}]}, NOW)
        self.assertIsNone(facts["observed_at"])
        self.assertIsNone(facts["observed_basis"])
        self.assertEqual(facts["freshness"], "unknown")
        self.assertIn("retrieval time is not freshness proof", facts["warnings"][0])

    def test_explicit_source_snapshot_time_drives_fresh_and_stale(self):
        def facts(age):
            return observation({"as_of": (NOW - age).isoformat(), "rows": []}, NOW)

        fresh = facts(timedelta(seconds=60))
        self.assertEqual((fresh["freshness"], fresh["observed_basis"]), ("fresh", "source_as_of"))
        with patch.object(settings, "evidence_stale_seconds", 900):
            stale = facts(timedelta(hours=3))
        self.assertEqual(stale["freshness"], "stale")
        self.assertTrue(any("old, beyond" in w for w in stale["warnings"]))

    def test_future_times_flag_clock_skew_and_never_claim_fresh(self):
        future = (NOW + timedelta(hours=2)).isoformat()
        skewed = observation({"as_of": future}, NOW)
        self.assertTrue(skewed["clock_skew_suspected"])
        self.assertEqual(skewed["freshness"], "unknown")
        self.assertTrue(observation({"a": [{"time_created": future}]}, NOW)["clock_skew_suspected"])

    def test_scan_is_bounded_for_large_or_deep_results(self):
        wide = [{"time_created": "2026-11-17T11:00:00+00:00", "x": i} for i in range(20000)]
        facts = observation(wide, NOW)
        self.assertLessEqual(facts["record_count"], 20000)
        deep = cur = {}
        for _ in range(500):
            cur["n"] = {}
            cur = cur["n"]
        self.assertIsNone(observation(deep, NOW)["observed_at"])

    def test_evidence_record_carries_observation_and_a_stable_id(self):
        data = {"alarms": [{"time_created": "2026-11-17 11:50:00"}]}
        first = evidence(Skill(), {"window_min": 10, "site": "STN-A"}, data)
        again = evidence(Skill(), {"window_min": 10}, data)
        self.assertEqual(first["evidence_id"], again["evidence_id"])
        self.assertTrue(first["evidence_id"].startswith("ev_"))
        self.assertEqual(first["observed_at"], "2026-11-17T11:50:00+00:00")
        self.assertEqual((first["entity"], first["time_window_minutes"]), ("STN-A", 10))
        self.assertNotEqual(first["observed_at"], first["retrieved_at"])


def tool(content, error=False):
    return NormalizedMessage(role="tool", tool_call_id="c", tool_name="t", content=content, is_error=error)


def result(ev=EV, text=""):
    import json

    return tool(json.dumps({"data": {"active_alarm_count": 1234, "rate": 12.5, "note": text}, "evidence": {"evidence_id": ev}}))


class AnswerCheckTest(unittest.TestCase):
    def test_grounded_answer_cites_a_real_id_and_uses_tool_numbers(self):
        report = check_answer(f"Có 1,234 alarm active, tỷ lệ 12.5% [{EV}].", [result()], "")
        self.assertEqual(report["status"], "grounded")
        self.assertEqual(report["cited"], [EV])
        self.assertEqual(report["unsupported_numbers"], [])

    def test_fabricated_reference_is_invalid_even_if_a_real_one_is_also_cited(self):
        report = check_answer(f"Theo {EV} và {OTHER}: 1234 alarm.", [result()], "")
        self.assertEqual(report["status"], "invalid_citation")
        self.assertEqual(report["unknown_ids"], [OTHER])
        self.assertEqual(report["cited"], [EV])

    def test_evidence_from_a_failed_tool_call_cannot_be_cited(self):
        errored = tool('{"data": null, "evidence": {"evidence_id": "%s"}}' % EV, error=True)
        self.assertEqual(check_answer(f"See {EV}", [errored], "")["status"], "invalid_citation")

    def test_answer_without_citation_is_uncited_when_tools_returned_evidence(self):
        self.assertEqual(check_answer("Có 1234 alarm.", [result()], "")["status"], "uncited")

    def test_no_tools_and_no_citations_is_not_applicable_but_a_citation_alone_is_invalid(self):
        self.assertEqual(check_answer("Xin chào", [], "")["status"], "not_applicable")
        self.assertEqual(check_answer(f"theo {EV}", [], "")["status"], "invalid_citation")

    def test_numbers_missing_from_tool_data_are_reported(self):
        report = check_answer(f"Có 9876 alarm và 12.5% [{EV}].", [result()], "")
        self.assertEqual(report["status"], "unsupported_numbers")
        self.assertEqual(report["unsupported_numbers"], ["9876"])

    def test_small_numbers_user_numbers_and_id_digits_are_not_claims(self):
        text = f"1. Có 3 mục trong 30 phút [{EV}] 1234 alarm"
        self.assertEqual(check_answer(text, [result()], "xem 30 phút")["status"], "grounded")

    def test_unsupported_numbers_are_capped(self):
        many = " ".join(str(1000 + i) for i in range(40))
        report = check_answer(f"{many} [{EV}]", [result()], "")
        self.assertEqual(len(report["unsupported_numbers"]), 10)


class RecordingExecutor:
    async def execute_call(self, call):
        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={"active_alarm_count": 1234},
            evidence={"evidence_id": EV},
        )


class EngineEnforcementTest(unittest.IsolatedAsyncioTestCase):
    async def run_engine(self, mode, answer):
        registry = SkillRegistry()
        provider = FakeProvider([
            {"text": "Querying", "calls": [("c1", "alarm_statistics", {})]},
            {"text": answer},
        ])
        with patch.object(settings, "evidence_enforcement", mode):
            engine = AgentEngine(RecordingExecutor(), registry)
            return [e async for e in engine.run("conv", "how many alarms?", provider, [])]

    async def test_warn_mode_reports_grounding_on_the_final_event(self):
        events = await self.run_engine("warn", "Có 1234 alarm.")
        self.assertEqual(events[-1].type, AgentEventType.FINAL_ANSWER)
        self.assertEqual(events[-1].data["grounding"]["status"], "uncited")
        events = await self.run_engine("warn", f"Có 1234 alarm [{EV}].")
        self.assertEqual(events[-1].data["grounding"]["status"], "grounded")

    async def test_strict_mode_refuses_fabricated_uncited_and_unsupported_answers(self):
        for answer, status in (
            (f"Theo {OTHER}: 1234", "invalid_citation"),
            ("Có 1234 alarm.", "uncited"),
            (f"Có 5678 alarm [{EV}]", "unsupported_numbers"),
        ):
            with self.subTest(status):
                events = await self.run_engine("strict", answer)
                self.assertEqual(events[-1].type, AgentEventType.ERROR)
                self.assertEqual(events[-1].data["code"], "ANSWER_UNGROUNDED")
                self.assertEqual(events[-1].data["grounding"]["status"], status)
                self.assertNotIn(AgentEventType.FINAL_ANSWER, [e.type for e in events])

    async def test_strict_mode_accepts_a_grounded_answer(self):
        events = await self.run_engine("strict", f"Có 1,234 alarm active [{EV}].")
        self.assertEqual(events[-1].type, AgentEventType.FINAL_ANSWER)

    async def test_off_mode_adds_nothing(self):
        events = await self.run_engine("off", "Có 1234 alarm.")
        self.assertNotIn("grounding", events[-1].data)


if __name__ == "__main__":
    unittest.main()
