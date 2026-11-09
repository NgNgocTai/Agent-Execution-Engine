import base64
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.skills.registry import SkillRegistry
from services.skill_catalog import ScriptSkill
from services.skill_validation import Manifest, ScriptSpec, inspect_archive


class ToolNamingTest(unittest.TestCase):
    def test_multiscript_names_are_bounded_unique_and_legacy_is_explicit(self):
        manifest = Manifest(
            name="custom_" + "a" * 56,
            version="1.0.0",
            description="Transform NOC alarm records",
            scripts=[ScriptSpec(entrypoint=f"scripts/{i}.py") for i in range(20)],
        )
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", manifest.model_dump_json())
            for spec in manifest.scripts:
                archive.writestr(spec.entrypoint, 'print("{}")')
        payload = buffer.getvalue()
        parsed, _, digest = inspect_archive(payload)
        record = NS(
            id="fixture",
            content_hash=digest,
            archive=base64.b64encode(payload).decode(),
            manifest=parsed.model_dump_json(),
            validation_report=None,
        )
        skills = [ScriptSkill(record, None, s.entrypoint) for s in manifest.scripts]
        self.assertEqual(len({s.name for s in skills}), 20)
        self.assertTrue(all(len(s.name) <= 64 for s in skills))
        self.assertEqual(
            [s.script_path for s in skills], [s.entrypoint for s in manifest.scripts]
        )
        self.assertEqual(
            len(
                ScriptSkill(
                    record, None, manifest.scripts[0].entrypoint, naming_version=1
                ).name
            ),
            73,
        )
        other_manifest = manifest.model_copy(
            update={"name": "custom_" + "a" * 55 + "b"}
        )
        other_buffer = BytesIO()
        with zipfile.ZipFile(other_buffer, "w") as archive:
            archive.writestr("manifest.json", other_manifest.model_dump_json())
            for spec in other_manifest.scripts:
                archive.writestr(spec.entrypoint, 'print("{}")')
        other_payload = other_buffer.getvalue()
        other_parsed, _, other_digest = inspect_archive(other_payload)
        other_record = NS(
            id="other",
            content_hash=other_digest,
            archive=base64.b64encode(other_payload).decode(),
            manifest=other_parsed.model_dump_json(),
            validation_report=None,
        )
        self.assertNotEqual(
            skills[0].name,
            ScriptSkill(other_record, None, other_manifest.scripts[0].entrypoint).name,
        )
        self.assertEqual(
            skills[0].name,
            ScriptSkill(record, None, manifest.scripts[0].entrypoint).name,
        )
        registry = SkillRegistry()
        registry.register(skills[0])
        with self.assertRaises(ValueError):
            registry.register(skills[0])
