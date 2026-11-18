from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.redaction import redact
from evals import adversarial

REQUIRED_CATEGORIES = {
    "tool_output_injection",
    "skill_reference_injection",
    "cross_site_read",
    "schema_explosion",
    "secret_leakage",
    "truncated_output",
    "instruction_gate_bypass",
    "fabricated_evidence",
}


class AdversarialDatasetTest(unittest.IsolatedAsyncioTestCase):
    def test_dataset_is_labelled_complete_and_unique(self):
        cases = adversarial.load_cases()
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        self.assertEqual({c["category"] for c in cases}, REQUIRED_CATEGORIES)
        for case in cases:
            self.assertIn(case["severity"], {"critical", "high", "medium"})
            self.assertTrue(case["attack"] and case["expect"])
            self.assertIn(case["category"], adversarial.HANDLERS)

    async def test_every_attack_is_contained(self):
        failed = {
            case["id"]: failures
            for case, _, failures in await adversarial.run_all()
            if failures
        }
        self.assertEqual(failed, {})

    async def run_case(self, case_id):
        case = next(c for c in adversarial.load_cases() if c["id"] == case_id)
        return (await adversarial.run_case(case))[1]


class EvalIsNotVacuousTest(AdversarialDatasetTest):
    """Removing a defence must turn the matching case red."""

    async def test_without_approval_gating_the_injection_cases_fail(self):
        with patch("services.skills.node_health_remediate.approved_snapshot", lambda metadata: True), \
             patch("core.policy.target_allowed", lambda *a, **k: True):
            self.assertTrue(await self.run_case("ADV-002"))

    async def test_without_site_authorization_the_cross_site_case_fails(self):
        with patch("services.site_capabilities.site_allowed", lambda actor, station: True):
            self.assertTrue(await self.run_case("ADV-005"))

    async def test_without_redaction_the_secret_case_fails(self):
        with patch("services.tool_executor.redact", lambda value: value), \
             patch("core.event_emitter.redact", lambda value: value, create=True):
            self.assertTrue(await self.run_case("ADV-012"))

    async def test_without_the_instruction_gate_the_bypass_case_fails(self):
        with patch("services.agent_service.instructions_loaded", lambda *a: True):
            self.assertTrue(await self.run_case("ADV-015"))

    async def test_without_output_validation_the_schema_case_fails(self):
        with patch("services.skill_validation.validate_value", lambda schema, value: value):
            self.assertTrue(await self.run_case("ADV-011"))


class RedactionFormatsTest(unittest.TestCase):
    def test_recognisable_credentials_are_masked_without_a_label(self):
        for secret in (
            "sk-live-0123456789abcdef0123456789abcdef",
            "sk-ant-api03-abcdefghijklmnopqrstuv",
            "AKIAABCDEFGHIJKLMNOP",
            "ghp_" + "a" * 36,
            "xoxb-1234567890-abcdefghij",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIz",
        ):
            with self.subTest(secret[:8]):
                self.assertNotIn(secret, redact(f"value {secret} end"))
        self.assertEqual(redact("my password is hunter2"), "my password is [REDACTED]")

    def test_ordinary_text_is_left_alone(self):
        text = "skeleton task-sk-1 ask-me asked AKIA lowercase ev_" + "a" * 20
        self.assertEqual(redact(text), text)


if __name__ == "__main__":
    unittest.main()
