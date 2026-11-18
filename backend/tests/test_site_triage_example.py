from io import BytesIO, StringIO
import contextlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings
from services.skill_pipeline import analyze
from services.skill_schema import validate_value
from services.skill_validation import inspect_archive

PACKAGE = Path(__file__).resolve().parents[2] / "skill_examples" / "site-alarm-triage"
SCRIPT = PACKAGE / "scripts" / "triage_site_alarms.py"


def archive():
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for path in sorted(PACKAGE.rglob("*")):
            if path.is_file():
                bundle.write(path, "site-alarm-triage/" + path.relative_to(PACKAGE).as_posix())
    return output.getvalue()


def alarm(i, severity="Critical", code="LINK_DOWN", device="d1"):
    return {"alarm_id": i, "error_code": code, "device_id": device, "severity": severity,
            "time_created": f"2026-11-17 10:{i:02d}:00", "status": "active"}


def run_script(arguments):
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "args.json").write_text(json.dumps(arguments), encoding="utf-8")
        previous, buffer = os.getcwd(), StringIO()
        os.chdir(directory)
        try:
            with contextlib.redirect_stdout(buffer):
                runpy.run_path(str(SCRIPT), run_name="__main__")
        finally:
            os.chdir(previous)
    return json.loads(buffer.getvalue())


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.manifest, self.files, self.digest = inspect_archive(archive())
        self.spec = self.manifest.run_specs()[0]

    def test_package_passes_static_review_and_declares_the_broker_not_data_access(self):
        self.assertEqual(self.manifest.name, "custom_site_alarm_triage")
        self.assertEqual((self.spec.capability, self.spec.capability_field), ("site_alarms", "alarms"))
        self.assertEqual(set(self.spec.tool_input_model().model_json_schema()["properties"]), {"site"})
        self.assertIn("references/scoring.md", self.files)

    def test_sample_input_and_every_run_validate_against_the_reviewed_schemas(self):
        self.spec.input_model().model_validate(self.spec.sample_input)
        for alarms in ([], [alarm(1)], [alarm(i, "Major", f"C{i}", f"d{i}") for i in range(1, 6)]):
            output = run_script({"site": "STN-A", "alarms": alarms})
            validate_value(self.spec.output_schema, output)


class ScoringTest(unittest.TestCase):
    def test_no_alarms_is_reported_as_none_observed_never_as_healthy(self):
        out = run_script({"site": "STN-A", "alarms": []})
        self.assertEqual((out["data_status"], out["impact_level"], out["score"]), ("no_alarms", "none_observed", 0))
        self.assertNotIn("health", json.dumps(out).lower())

    def test_severity_drives_score_and_levels(self):
        low = run_script({"site": "s", "alarms": [alarm(1, "Warning")]})
        medium = run_script({"site": "s", "alarms": [alarm(1, "Major"), alarm(2, "Major")]})
        high = run_script({"site": "s", "alarms": [alarm(1), alarm(2), alarm(3, "Major")]})
        self.assertEqual((low["impact_level"], low["score"]), ("low", 4.0))
        self.assertEqual((medium["impact_level"], medium["score"]), ("medium", 40.0))
        self.assertEqual((high["impact_level"], high["score"]), ("high", 100.0))
        self.assertIn("2 major alarm(s)", medium["drivers"])

    def test_one_noisy_severity_cannot_exceed_its_cap_and_score_is_capped(self):
        out = run_script({"site": "s", "alarms": [alarm(i) for i in range(1, 11)]})
        self.assertEqual(out["score"], 100.0)
        warning = run_script({"site": "s", "alarms": [alarm(i, "Warning") for i in range(1, 11)]})
        self.assertEqual(warning["score"], 12.0)

    def test_spread_bonuses_and_top_codes_are_deterministic(self):
        alarms = [alarm(1, "Minor", "A", "d1"), alarm(2, "Minor", "B", "d2"), alarm(3, "Minor", "A", "d3")]
        out = run_script({"site": "s", "alarms": alarms})
        self.assertEqual(out["score"], 8 * 3 + 10)
        self.assertEqual(out["top_error_codes"][0], {"error_code": "A", "count": 2})
        self.assertEqual(out["distinct_devices"], 3)
        self.assertEqual(run_script({"site": "s", "alarms": alarms}), out)

    def test_unknown_severity_adds_no_weight_and_is_flagged(self):
        out = run_script({"site": "s", "alarms": [alarm(1, "Catastrophic"), alarm(2, "Minor")]})
        self.assertEqual(out["severity_counts"]["unknown"], 1)
        self.assertEqual(out["score"], 8.0)
        self.assertTrue(any("unrecognised severity" in w for w in out["warnings"]))

    def test_full_list_is_flagged_as_a_lower_bound(self):
        out = run_script({"site": "s", "alarms": [alarm(i % 50 + 1, "Warning", device=f"d{i}") for i in range(100)]})
        self.assertTrue(any("lower bound" in w for w in out["warnings"]))

    def test_missing_records_fail_instead_of_falling_back_to_sample_data(self):
        with self.assertRaises(KeyError):
            run_script({"site": "s"})

    def test_output_makes_no_root_cause_claim(self):
        out = run_script({"site": "s", "alarms": [alarm(1)]})
        self.assertIn("not a root cause", out["interpretation"])
        self.assertEqual(out["calibration"], "uncalibrated-v0")


@unittest.skipUnless(os.environ.get("SANDBOX_TEST_IMAGE"), "Reviewed local sandbox image is not configured")
class SandboxReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_package_passes_the_real_sandbox_smoke_test(self):
        from services.sandbox_executor import execute_sandbox

        manifest, files, digest = inspect_archive(archive())
        with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]):
            _, report = await analyze(manifest, files, digest, runner=execute_sandbox)
        self.assertTrue(report["ready_for_review"], report["scripts"])
        entry = report["scripts"][0]
        self.assertEqual(entry["capability"], "site_alarms")
        self.assertEqual(entry["result"]["data"]["impact_level"], "medium")


if __name__ == "__main__":
    unittest.main()
