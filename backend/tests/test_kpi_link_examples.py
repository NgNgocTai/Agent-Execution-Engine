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

EXAMPLES = Path(__file__).resolve().parents[2] / "skill_examples"
RAN = EXAMPLES / "ran-kpi-triage"
LINK = EXAMPLES / "core-link-impact"


def archive(package):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, package.name + "/" + path.relative_to(package).as_posix())
    return output.getvalue()


def run_script(package, script, arguments):
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "args.json").write_text(json.dumps(arguments), encoding="utf-8")
        previous, buffer = os.getcwd(), StringIO()
        os.chdir(directory)
        try:
            with contextlib.redirect_stdout(buffer):
                runpy.run_path(str(package / "scripts" / script), run_name="__main__")
        finally:
            os.chdir(previous)
    return json.loads(buffer.getvalue())


def ran(records):
    return run_script(RAN, "triage_ran_kpis.py", {"records": records})


def link(records):
    return run_script(LINK, "summarize_link_impact.py", {"records": records})


def bad_cell(cell_id="C1", **kpis):
    return {"cell_id": cell_id, "availability_pct": 90.0, "drop_rate_pct": 8.0, "latency_ms": 200,
            "packet_loss_pct": 5.0, "throughput_mbps": 5, "affected_subscribers": 900, **kpis}


class PackageTest(unittest.TestCase):
    def check(self, package, name):
        manifest, files, _ = inspect_archive(archive(package))
        spec = manifest.run_specs()[0]
        self.assertEqual(manifest.name, name)
        self.assertIsNone(spec.capability)  # data comes from the operator, never a broker
        self.assertEqual(set(spec.tool_input_model().model_json_schema()["properties"]), {"records"})
        spec.input_model().model_validate(spec.sample_input)
        self.assertEqual([p for p in files if p.startswith("references/")].__len__(), 1)
        return spec

    def test_ran_package_passes_static_review(self):
        spec = self.check(RAN, "custom_ran_kpi_triage")
        out = ran(spec.sample_input["records"])
        validate_value(spec.output_schema, out)

    def test_link_package_passes_static_review(self):
        spec = self.check(LINK, "custom_core_link_impact")
        out = link(spec.sample_input["records"])
        validate_value(spec.output_schema, out)

    def test_inputs_reject_values_outside_declared_bounds(self):
        manifest, _, _ = inspect_archive(archive(RAN))
        model = manifest.run_specs()[0].input_model()
        for bad in ({"cell_id": "c", "availability_pct": 140}, {"cell_id": "c", "latency_ms": -1},
                    {"cell_id": ""}, {"cell_id": "c", "bogus": 1}):
            with self.assertRaises(Exception):
                model.model_validate({"records": [bad]})
        with self.assertRaises(Exception):
            model.model_validate({"records": []})


class RanTriageTest(unittest.TestCase):
    def test_breached_kpis_rank_above_healthy_cells_with_drivers(self):
        out = ran([{"cell_id": "OK", "availability_pct": 99.9, "drop_rate_pct": 0.2, "latency_ms": 20,
                    "packet_loss_pct": 0.1, "throughput_mbps": 90, "affected_subscribers": 10},
                   bad_cell("BAD", site_id="S1")])
        self.assertEqual([c["cell_id"] for c in out["ranked_cells"]], ["BAD", "OK"])
        top, ok = out["ranked_cells"]
        self.assertEqual((top["severity"], top["score"]), ("critical", 100.0))
        self.assertTrue(any("availability below target" in d for d in top["drivers"]))
        self.assertEqual((ok["severity"], ok["score"]), ("normal", 0.0))
        self.assertEqual(out["summary"]["top_site_by_total_score"], "S1")

    def test_a_missing_kpi_is_never_assumed_healthy(self):
        out = ran([{"cell_id": "P", "availability_pct": 99.5}])
        cell = out["ranked_cells"][0]
        self.assertEqual((cell["severity"], cell["kpis_reported"]), ("normal", 1))
        self.assertTrue(any("5 KPI(s) not reported" in d for d in cell["drivers"]))
        self.assertTrue(any("only some KPIs" in w for w in out["warnings"]))

    def test_a_cell_with_no_kpi_is_insufficient_data_not_normal(self):
        out = ran([{"cell_id": "E", "site_id": "S"}])
        self.assertEqual(out["ranked_cells"][0]["severity"], "insufficient_data")
        self.assertEqual(out["summary"]["severity_counts"]["insufficient_data"], 1)
        self.assertNotIn("top_site_by_total_score", out["summary"])

    def test_zero_throughput_is_scored_as_the_severe_reading_it_is(self):
        zero = ran([{"cell_id": "Z", "throughput_mbps": 0}])["ranked_cells"][0]
        self.assertEqual((zero["score"], zero["severity"]), (50.0, "major"))
        self.assertIn("throughput low: 0.0 Mbps", zero["drivers"])
        self.assertTrue(any("possible outage" in d for d in zero["drivers"]))
        low = ran([{"cell_id": "L", "throughput_mbps": 1}])["ranked_cells"][0]
        self.assertEqual((low["score"], low["severity"]), (22.0, "normal"))  # capped, and the floor is for 0 only

    def test_no_samples_are_substituted_when_records_are_missing(self):
        with self.assertRaises(KeyError):
            run_script(RAN, "triage_ran_kpis.py", {})

    def test_output_is_deterministic_and_bounded_with_a_truncation_warning(self):
        records = [bad_cell(f"C{i:03d}") for i in range(120)]
        out = ran(records)
        self.assertEqual(len(out["ranked_cells"]), 50)
        self.assertEqual(out["summary"]["total_cells"], 120)
        self.assertTrue(any("highest-scoring of 120" in w for w in out["warnings"]))
        self.assertEqual(ran(records), out)

    def test_duplicates_freshness_and_no_root_cause(self):
        out = ran([{"cell_id": "D", "availability_pct": 99}, {"cell_id": "D", "availability_pct": 98,
                   "observed_at": "2026-11-17T10:00:00Z"}])
        self.assertTrue(any("Duplicate cell_id" in w for w in out["warnings"]))
        self.assertTrue(any("no observed_at" in w for w in out["warnings"]))
        self.assertEqual(out["newest_observed_at"], "2026-11-17T10:00:00Z")
        self.assertEqual((out["input_source"], out["calibration"]), ("operator_supplied", "uncalibrated-v0"))
        self.assertIn("not a root cause", out["interpretation"])
        self.assertNotIn("health", json.dumps(out["interpretation"]).lower())


def alarm(link_id="L1", severity="critical", **extra):
    return {"link_id": link_id, "severity": severity, **extra}


class LinkImpactTest(unittest.TestCase):
    def test_blast_radius_and_degradation_drive_the_ranking(self):
        out = link([
            alarm("L1", "critical", site_id="A", service="data", latency_ms=145, packet_loss_pct=4.1, throughput_mbps=60),
            alarm("L1", "major", site_id="B", service="voice"),
            alarm("L2", "minor", site_id="C", service="vpn")])
        first, second = out["impacts"]
        self.assertEqual((first["link_id"], first["impact_level"]), ("L1", "high"))
        self.assertEqual((first["site_count"], first["service_count"]), (2, 2))
        self.assertEqual((second["link_id"], second["impact_level"]), ("L2", "low"))
        self.assertEqual(out["summary"]["top_link"], "L1")
        self.assertTrue(any("latency peak 145 ms" in d for d in first["drivers"]))

    def test_unknown_severity_adds_no_weight_and_is_not_read_as_normal(self):
        out = link([alarm("L1", "catastrophic")])
        item = out["impacts"][0]
        self.assertEqual((item["impact_score"], item["impact_level"]), (0.0, "insufficient_data"))
        self.assertEqual(item["severity_counts"]["unknown"], 1)
        self.assertTrue(any("unrecognised severity" in w for w in out["warnings"]))
        self.assertNotIn("top_link", out["summary"])

    def test_cleared_records_are_none_observed_not_a_health_claim(self):
        item = link([alarm("L1", "cleared")])["impacts"][0]
        self.assertEqual(item["impact_level"], "none_observed")
        self.assertNotIn("health", json.dumps(item).lower())

    def test_zero_throughput_is_scored(self):
        item = link([alarm("L1", "normal", throughput_mbps=0)])["impacts"][0]
        self.assertEqual((item["impact_score"], item["impact_level"]), (45.0, "medium"))
        self.assertTrue(any("possible link failure" in d for d in item["drivers"]))
        slow = link([alarm("L1", "normal", throughput_mbps=1)])["impacts"][0]
        self.assertEqual((slow["impact_score"], slow["impact_level"]), (20.0, "low"))

    def test_missing_sites_and_services_are_lower_bounds_not_counted_as_unknown_sites(self):
        out = link([alarm("L1", "major"), alarm("L1", "major")])
        self.assertEqual(out["impacts"][0]["site_count"], 0)
        self.assertTrue(any("impacted sites are a lower bound" in w for w in out["warnings"]))
        self.assertTrue(any("affected services are a lower bound" in w for w in out["warnings"]))

    def test_aliases_are_normalised(self):
        counts = link([alarm("L1", "P1"), alarm("L1", "Maj"), alarm("L1", "WARN")])["summary"]["severity_counts"]
        self.assertEqual((counts["critical"], counts["major"], counts["warning"]), (1, 1, 1))

    def test_lists_are_bounded_and_output_is_deterministic(self):
        records = [alarm(f"L{i:03d}", "major", site_id=f"S{i}", alarm_id=f"A{i}") for i in range(80)]
        one = alarm("BIG", "major", site_id="X")
        many = [dict(one, site_id=f"S{i}", alarm_id=f"A{i}") for i in range(60)]
        out = link(records + many)
        self.assertEqual(len(out["impacts"]), 50)
        self.assertTrue(any("highest-impact of 81 links" in w for w in out["warnings"]))
        big = next(i for i in link(many)["impacts"])
        self.assertEqual((big["site_count"], len(big["sites"]), len(big["alarm_ids"])), (60, 10, 10))
        self.assertEqual(link(records + many), out)

    def test_missing_records_fail_instead_of_falling_back_to_sample_data(self):
        with self.assertRaises(KeyError):
            run_script(LINK, "summarize_link_impact.py", {})


@unittest.skipUnless(os.environ.get("SANDBOX_TEST_IMAGE"), "Reviewed local sandbox image is not configured")
class SandboxReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_packages_pass_the_real_sandbox_smoke_test(self):
        from services.sandbox_executor import execute_sandbox

        for package in (RAN, LINK):
            manifest, files, digest = inspect_archive(archive(package))
            with patch.object(settings, "sandbox_image", os.environ["SANDBOX_TEST_IMAGE"]):
                _, report = await analyze(manifest, files, digest, runner=execute_sandbox)
            self.assertTrue(report["ready_for_review"], (package.name, report["scripts"]))


if __name__ == "__main__":
    unittest.main()
