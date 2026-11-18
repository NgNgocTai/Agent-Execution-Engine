import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from controllers import skills
from core.auth import Principal, _principal
from core.schemas import NormalizedResponse
from repositories.models import Base
from repositories.skill_models import SkillVersion
from services.skill_ast import analyze_python
from services.skill_catalog import ScriptSkill, public_version, resolved_manifest
from services.skill_pipeline import analyze, initial_report, normalize_output
from services.skill_validation import inspect_archive, SkillValidationError
from services.skills.base import SkillResult, SkillStatus
from test_skill_safety import archive


def markdown_package(
    extra=None,
    header="name: alarm-summary\ndescription: Summarize telecom alarm data",
    wrapper="",
):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            wrapper + "SKILL.md",
            f"---\n{header}\n---\nSummarize supplied alarm observations; state uncertainty.",
        )
        for path, content in (extra or {}).items():
            bundle.writestr(wrapper + path, content)
    return output.getvalue()


class AstAndPackageTest(unittest.TestCase):
    def test_detailed_ast_rejects_reflection_aliases_and_file_bypasses(self):
        for code in (
            "import sys as s\ns.modules['os']",
            "from sys import modules as m",
            "x.__class__.__mro__",
            "runner = eval",
            "getattr(x, 'run')()",
            "from json import *",
            "import pathlib as p",
            "open(path)",
            "open('data.csv', 'w')",
            "open('args.json', opener=custom)",
            "open('..\\\\secret')",
            "open('/etc/passwd')",
            "import sys\nsys._getframe()",
            "import sys\nsys.environ",
        ):
            with self.subTest(code=code):
                findings = analyze_python(code, "scripts/test.py")
                self.assertTrue(findings)
                self.assertEqual(findings[0]["path"], "scripts/test.py")
                self.assertGreater(findings[0]["line"], 0)
                self.assertIn("code", findings[0])

    def test_safe_streams_and_literal_workspace_read_are_allowed(self):
        self.assertEqual(
            analyze_python(
                "import json, sys\nwith open('args.json', mode='r', encoding='utf-8') as f:\n print(json.load(f))",
                "main.py",
            ),
            [],
        )

    def test_static_failure_has_structured_evidence(self):
        with self.assertRaises(SkillValidationError) as raised:
            inspect_archive(archive(code="import sys\nsys.modules['os']"))
        self.assertEqual(raised.exception.report["status"], "failed")
        self.assertTrue(raised.exception.report["findings"])

    def test_markdown_wrapper_multiscript_and_explicit_specs(self):
        runtime = {
            "scripts": [
                {"entrypoint": "scripts/one.py", "output_contract": "json"},
                {"entrypoint": "scripts/two.py", "output_contract": "text"},
            ]
        }
        manifest, files, _ = inspect_archive(
            markdown_package(
                {
                    "runtime.json": json.dumps(runtime),
                    "scripts/one.py": "print('{}')",
                    "scripts/two.py": "print('ready')",
                },
                wrapper="alarm-summary/",
            )
        )
        self.assertEqual(manifest.name, "custom_alarm_summary")
        self.assertEqual(
            [spec.output_contract for spec in manifest.run_specs()], ["json", "text"]
        )
        self.assertIn("SKILL.md", files)

    def test_yaml_aliases_duplicate_keys_and_undeclared_runtime_paths_fail(self):
        for header in (
            "name: alarm-summary\nname: other\ndescription: Telecom alarm summary",
            "name: &name alarm-summary\ndescription: *name",
            "name: alarm-summary\ndescription: !!python/object:x {}",
            "name: alarm-summary\ndescription: Telecom alarm summary\nnetwork: host",
        ):
            with self.subTest(header=header), self.assertRaises(ValueError):
                inspect_archive(markdown_package(header=header))
        with self.assertRaises(ValueError):
            inspect_archive(
                markdown_package(
                    {"scripts/one.py": "pass", "runtime.json": '{"scripts":[]}'}
                )
            )

    def test_output_contract_never_grants_privileged_execution(self):
        with self.assertRaises(ValueError):
            normalize_output(b'{"count":NaN}', "json")
        self.assertEqual(normalize_output(b'{"count":2}', "json").data, {"count": 2})
        self.assertEqual(normalize_output(b"hello", "text").data, "hello")
        with self.assertRaises(ValueError):
            normalize_output(
                b'{"status":"success","data":{},"needs_approval":true}', "skill_result"
            )


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_script_must_pass_and_offline_does_not_call_llm(self):
        manifest, files, digest = inspect_archive(
            markdown_package(
                {"scripts/one.py": "print('{}')", "scripts/two.py": "print('{}')"}
            )
        )
        runner = AsyncMock(
            side_effect=[
                SkillResult(status=SkillStatus.SUCCESS, data={}),
                ValueError("private remote detail"),
            ]
        )
        _, report = await analyze(manifest, files, digest, runner=runner)
        self.assertEqual(runner.await_count, 2)
        self.assertEqual(
            [entry["status"] for entry in report["scripts"]], ["passed", "failed"]
        )
        self.assertFalse(report["ready_for_review"])
        self.assertNotIn("private remote detail", json.dumps(report))
        self.assertEqual(
            next(stage for stage in report["stages"] if stage["stage"] == "llm")[
                "status"
            ],
            "not_requested",
        )

    async def test_llm_proposals_are_typed_advisory_and_smoke_tested(self):
        manifest, files, digest = inspect_archive(
            markdown_package({"scripts/one.py": "print('{}')"})
        )

        class Provider:
            async def chat_completion(self, messages, tools):
                yield NormalizedResponse(
                    text_content=json.dumps(
                        {
                            "domain_score": 0.8,
                            "reason": "Alarm processing",
                            "scripts": [
                                {
                                    "entrypoint": "scripts/one.py",
                                    "input_fields": [
                                        {"name": "text", "kind": "string"}
                                    ],
                                    "sample_input": {"text": "alarm"},
                                    "output_contract": "json",
                                }
                            ],
                        }
                    )
                )

        runner = AsyncMock(
            return_value=SkillResult(status=SkillStatus.SUCCESS, data={})
        )
        effective, report = await analyze(
            manifest, files, digest, provider=Provider(), runner=runner
        )
        self.assertTrue(report["ready_for_review"])
        self.assertEqual(effective.scripts[0].output_contract, "json")
        self.assertEqual(runner.call_args.args[2], {"text": "alarm"})
        self.assertEqual(report["stages"][-1]["status"], "pending")

    async def test_llm_cannot_propose_unbundled_scripts_or_bypass_domain_review(self):
        manifest, files, digest = inspect_archive(
            markdown_package(
                {"scripts/one.py": "print('{}')"},
                header="name: utility\ndescription: Format supplied numbers",
            )
        )
        manifest.instructions = "Format supplied numbers."

        class Provider:
            async def chat_completion(self, messages, tools):
                yield NormalizedResponse(
                    text_content='{"domain_score":1.0,"reason":"approved","scripts":[{"entrypoint":"evil.py"}]}'
                )

        _, report = await analyze(
            manifest,
            files,
            digest,
            provider=Provider(),
            runner=AsyncMock(
                return_value=SkillResult(status=SkillStatus.SUCCESS, data={})
            ),
        )
        self.assertFalse(report["ready_for_review"])
        self.assertEqual(
            next(stage for stage in report["stages"] if stage["stage"] == "llm")[
                "status"
            ],
            "failed",
        )


class PipelineApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_finishing_after_revocation_cannot_restore_the_version(self):
        async def revoke_during_smoke(*args):
            async with self.sessions() as session:
                record = await session.get(SkillVersion, "version")
                record.status = "revoked"
                await session.commit()
            return SkillResult(status=SkillStatus.SUCCESS, data={})

        with patch.object(skills, "execute_sandbox", side_effect=revoke_during_smoke):
            with self.assertRaises(HTTPException) as raised:
                await skills.analyze_version(
                    "version", skills.Analyze(content_hash=self.digest)
                )
        self.assertEqual(raised.exception.status_code, 409)
        async with self.sessions() as session:
            self.assertEqual(
                (await session.get(SkillVersion, "version")).status, "revoked"
            )

    async def test_active_package_registers_each_script_and_runs_the_selected_entrypoint(
        self,
    ):
        from services.run_service import create_run
        from services.skill_catalog import registry_for_run
        from services.skills.registry import SkillRegistry

        with patch.object(
            skills,
            "execute_sandbox",
            return_value=SkillResult(status=SkillStatus.SUCCESS, data={}),
        ):
            version = await skills.analyze_version(
                "version", skills.Analyze(content_hash=self.digest)
            )
            await skills.review(
                "version",
                skills.Review(
                    content_hash=self.digest,
                    report_hash=version["report_hash"],
                    approved=True,
                    notes="Reviewed both alarm scripts",
                ),
            )
        with patch.object(skills, "sandbox_ready", return_value=True):
            await skills.activate("version")
        async with self.sessions() as session:
            run = await create_run(
                session,
                message="Summarize alarms",
                provider="mock",
                conversation_id=None,
                idempotency_key="dynamic-skill",
            )
            registry = await registry_for_run(
                SkillRegistry(), session, self.sessions, run
            )
            await session.commit()
        names = [n for n in registry.list_names() if n not in {"load_skill", "read_skill_file"}]
        self.assertEqual(len(names), 2)
        self.assertIn("load_skill", registry.list_names())
        for name in names:
            tool = registry.get(name)
            with patch(
                "services.skill_catalog.execute_sandbox",
                return_value=SkillResult(status=SkillStatus.SUCCESS, data={}),
            ) as sandbox:
                await tool.execute()
            self.assertEqual(sandbox.call_args.args[0].entrypoint, tool.script_path)

    async def test_instruction_only_agent_skill_does_not_start_a_sandbox(self):
        payload = markdown_package()
        manifest, files, digest = inspect_archive(payload)
        _, report = await analyze(
            manifest,
            files,
            digest,
            runner=AsyncMock(side_effect=AssertionError("must not execute")),
        )
        self.assertTrue(report["ready_for_review"])
        self.assertFalse(report["scripts"])

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = create_async_engine(
            "sqlite+aiosqlite:///" + Path(directory.name, "skills.db").as_posix()
        )
        self.addAsyncCleanup(self.db.dispose)
        async with self.db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db, expire_on_commit=False)
        self.enterContext(patch.object(skills, "async_session", self.sessions))
        context = _principal.set(
            Principal("reviewer", frozenset({"skill_reviewer", "admin"}))
        )
        self.addCleanup(_principal.reset, context)
        self.payload = markdown_package(
            {"scripts/one.py": "print('{}')", "scripts/two.py": "print('{}')"}
        )
        manifest, files, digest = inspect_archive(self.payload)
        self.digest = digest
        async with self.sessions() as session:
            session.add(
                SkillVersion(
                    id="version",
                    name=manifest.name,
                    version=manifest.version,
                    content_hash=digest,
                    manifest=manifest.model_dump_json(),
                    archive=base64.b64encode(self.payload).decode(),
                    status="quarantined",
                    uploaded_by="uploader",
                    validation_report=json.dumps(
                        initial_report(manifest, files, digest)
                    ),
                )
            )
            await session.commit()

    async def test_inspect_exposes_static_findings_without_upload_or_execution(self):
        app = FastAPI()
        app.include_router(skills.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with patch.object(skills, "execute_sandbox") as sandbox:
                response = await client.post(
                    "/api/skills/inspect",
                    content=archive(code="import sys\nsys.modules['os']"),
                )
            sandbox.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["report"]["findings"][0]["path"], "main.py"
        )

    async def test_stale_report_cannot_approve_and_multi_script_specs_are_pinned(self):
        with patch.object(
            skills,
            "execute_sandbox",
            return_value=SkillResult(status=SkillStatus.SUCCESS, data={}),
        ):
            version = await skills.analyze_version(
                "version", skills.Analyze(content_hash=self.digest)
            )
            with self.assertRaises(HTTPException) as raised:
                await skills.review(
                    "version",
                    skills.Review(
                        content_hash=self.digest,
                        report_hash="0" * 64,
                        approved=True,
                        notes="Reviewed both alarm scripts",
                    ),
                )
            self.assertEqual(raised.exception.status_code, 409)
            result = await skills.review(
                "version",
                skills.Review(
                    content_hash=self.digest,
                    report_hash=version["report_hash"],
                    approved=True,
                    notes="Reviewed both alarm scripts",
                ),
            )
        self.assertEqual(result["status"], "approved")
        async with self.sessions() as session:
            record = await session.get(SkillVersion, "version")
            one = ScriptSkill(record, self.sessions, "scripts/one.py")
            two = ScriptSkill(record, self.sessions, "scripts/two.py")
            self.assertNotEqual(one.name, two.name)
            manifest = json.loads(record.manifest)
            manifest["scripts"][0]["timeout_seconds"] = 2
            record.manifest = json.dumps(manifest)
            with self.assertRaises(ValueError):
                resolved_manifest(record)

    async def test_failed_smoke_is_persisted_and_does_not_approve(self):
        async with self.sessions() as session:
            version = public_version(await session.get(SkillVersion, "version"))
        with patch.object(
            skills, "execute_sandbox", side_effect=ValueError("not a valid output")
        ):
            with self.assertRaises(HTTPException) as raised:
                await skills.review(
                    "version",
                    skills.Review(
                        content_hash=self.digest,
                        report_hash=version["report_hash"],
                        approved=True,
                        notes="Inspected the two alarm scripts",
                    ),
                )
        self.assertEqual(raised.exception.status_code, 422)
        async with self.sessions() as session:
            record = await session.get(SkillVersion, "version")
            self.assertEqual(record.status, "quarantined")
            self.assertEqual(
                [
                    entry["status"]
                    for entry in json.loads(record.validation_report)["scripts"]
                ],
                ["failed", "failed"],
            )
