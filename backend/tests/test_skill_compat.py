from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.skill_pipeline import compatibility_stage, initial_report
from services.skill_validation import SkillValidationError, inspect_archive
from test_skill_pipeline import markdown_package

PEER = Path("D:/Code/VDT/Telecom-Agent-Execution-engine/Agent_skill")
OPENAI_YAML = "interface:\n  display_name: RAN triage\n  short_description: Rank cells\n"


def raw_package(entries, wrapper=""):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            wrapper + "SKILL.md",
            "---\nname: ran-triage\ndescription: Rank RAN cells by KPI degradation\n---\nUse the script.",
        )
        for path, content in entries.items():
            bundle.writestr(wrapper + path, content)
    return output.getvalue()


class ImportedMetadataTest(unittest.TestCase):
    def test_agent_metadata_and_inert_shell_are_kept_and_explained(self):
        payload = markdown_package(
            {
                "agents/openai.yaml": OPENAI_YAML,
                "references/guide.md": "Reference text",
                "scripts/run.py": "print('{}')",
                "scripts/probe.sh": "#!/bin/sh\necho hello\n",
            },
            wrapper="ran-triage/",
        )
        manifest, files, digest = inspect_archive(payload)
        self.assertIn("scripts/probe.sh", files)
        self.assertEqual([s.entrypoint for s in manifest.run_specs()], ["scripts/run.py"])
        stage = initial_report(manifest, files, digest)["stages"]
        compatibility = next(s for s in stage if s["stage"] == "compatibility")
        self.assertEqual(compatibility["inert_resources"], ["scripts/probe.sh"])
        self.assertEqual(compatibility["metadata"], {"agents/openai.yaml": ["interface"]})
        text = " ".join(compatibility["notes"])
        self.assertIn("never executed", text)
        self.assertIn("grants no tools", text)

    def test_plain_package_reports_nothing_to_explain(self):
        _, files, _ = inspect_archive(markdown_package({"scripts/run.py": "print('{}')"}))
        stage = compatibility_stage(files)
        self.assertEqual((stage["inert_resources"], stage["metadata"], stage["notes"]), ([], {}, []))

    def test_shell_or_yaml_cannot_be_a_declared_entrypoint(self):
        for entrypoint, files in (
            ("scripts/probe.sh", {"scripts/probe.sh": "echo hi"}),
            ("agents/openai.yaml", {"agents/openai.yaml": OPENAI_YAML}),
        ):
            runtime = {"scripts": [{"entrypoint": entrypoint}]}
            with self.subTest(entrypoint), self.assertRaises(ValueError):
                inspect_archive(markdown_package({**files, "runtime.json": json.dumps(runtime)}))

    def test_unsafe_or_misplaced_yaml_is_rejected(self):
        cases = {
            "alias": "a: &x 1\nb: *x\n",
            "tag": "a: !!python/object/apply:os.system ['id']\n",
            "duplicate keys": "a: 1\na: 2\n",
            "not a mapping": "- one\n- two\n",
            "oversized": "a: " + "x" * 20000 + "\n",
            "syntax": "a: [unclosed\n",
        }
        for name, content in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                inspect_archive(markdown_package({"agents/openai.yaml": content}))
        for path in ("config.yaml", "scripts/settings.yml", "agents/nested/x.yaml"):
            with self.subTest(path), self.assertRaises(ValueError):
                inspect_archive(markdown_package({path: OPENAI_YAML}))

    def test_secrets_in_inert_files_are_still_caught(self):
        payload = markdown_package(
            {"scripts/probe.sh": "export API_KEY=sk-live-0123456789abcdef0123456789abcdef\n"}
        )
        with self.assertRaises(SkillValidationError) as raised:
            inspect_archive(payload)
        self.assertEqual(raised.exception.report["findings"][0]["code"], "POTENTIAL_SECRET")


@unittest.skipUnless(PEER.is_dir(), "reference skill packages are not available")
class ReferencePackagesTest(unittest.TestCase):
    def archive(self, name):
        return (PEER / f"{name}.zip").read_bytes()

    def test_ran_and_core_link_packages_import_with_metadata(self):
        for name in ("ran-kpi-anomaly-triage", "core-link-impact-summary"):
            with self.subTest(name):
                manifest, files, digest = inspect_archive(self.archive(name))
                self.assertEqual(len(manifest.run_specs()), 1)
                compatibility = compatibility_stage(files)
                self.assertIn("agents/openai.yaml", compatibility["metadata"])

    def test_dangerous_packages_are_still_rejected(self):
        for name in ("unsafe-node-debug-shell", "node-health-autoremediate"):
            with self.subTest(name), self.assertRaises(SkillValidationError):
                inspect_archive(self.archive(name))


if __name__ == "__main__":
    unittest.main()
