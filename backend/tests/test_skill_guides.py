import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from core.auth import Principal, _principal
from core.schemas import (
    AgentEventType,
    NormalizedResponse,
    NormalizedToolCall,
)
from repositories.models import Base
from repositories.skill_models import SkillActivation, SkillVersion
from services.agent_service import AgentEngine
from services.skill_catalog import (
    ScriptSkill,
    registry_for_run,
    requestable_skill,
)
from services.skill_guides import instructions_loaded
from services.skill_validation import inspect_archive
from services.skills.registry import SkillRegistry
from services.skills.alarm_statistics import AlarmStatisticsSkill
from services.tool_executor import ToolExecutor
from services.workflow.state import CheckpointIncompatible  # noqa: F401

MARKER = "CRITICAL-STEP-AFTER-LIMIT"


def long_instructions(marker=MARKER, extra=""):
    filler = ("Bước xử lý chi tiết cho kỹ sư NOC. " * 400)[:15000]
    return filler + f"\n{marker}: verify the site before querying.{extra}\n" + "x" * 3000


def package(name="alarm-triage", instructions=None, files=None, scripts=("scripts/a.py",)):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: Triage telecom alarm records\n---\n"
            + (instructions or long_instructions()),
        )
        for path in scripts:
            bundle.writestr(path, "print('{}')")
        for path, content in (files or {}).items():
            bundle.writestr(path, content)
    return output.getvalue()


class GuideFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = create_async_engine(
            "sqlite+aiosqlite:///" + Path(directory.name, "guides.db").as_posix()
        )
        self.addAsyncCleanup(self.db.dispose)
        async with self.db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db, expire_on_commit=False)
        context = _principal.set(
            Principal("op", frozenset({"operator"}), frozenset({"*"}), frozenset({"*"}))
        )
        self.addCleanup(_principal.reset, context)

    async def add_version(self, version_id, payload, status="approved", activate=True):
        manifest, _, digest = inspect_archive(payload)
        async with self.sessions() as session:
            session.add(
                SkillVersion(
                    id=version_id,
                    name=manifest.name,
                    version=manifest.version,
                    content_hash=digest,
                    manifest=manifest.model_dump_json(),
                    archive=base64.b64encode(payload).decode(),
                    status=status,
                    uploaded_by="uploader",
                )
            )
            await session.flush()
            if activate:
                existing = await session.get(SkillActivation, manifest.name)
                if existing:
                    existing.version_id = version_id
                else:
                    session.add(
                        SkillActivation(name=manifest.name, version_id=version_id)
                    )
            await session.commit()
        return manifest

    async def registry(self, run, base=None):
        async with self.sessions() as session:
            registry = await registry_for_run(
                base or SkillRegistry(), session, self.sessions, run
            )
            await session.commit()
        return registry

    def new_run(self, requested=None):
        return NS(selected_skill_versions="{}", requested_skill=requested)

    async def call(self, registry, tool, **arguments):
        return await ToolExecutor(registry).execute(tool, arguments)


class OnDemandInstructionsTest(GuideFixture):
    async def test_steps_after_3000_characters_are_readable_in_bounded_chunks(self):
        await self.add_version("v1", package())
        registry = await self.registry(self.new_run())
        offset, text, chunks = 0, "", 0
        while offset is not None:
            result = await self.call(
                registry, "load_skill", skill_name="custom_alarm_triage", offset=offset
            )
            self.assertTrue(result.success, result.error)
            self.assertFalse(result.data.get("truncated") and "content" not in result.data)
            text += result.data["content"]
            offset, chunks = result.data["next_offset"], chunks + 1
            self.assertIn("untrusted", result.data["notice"].lower())
        self.assertGreater(chunks, 1)
        self.assertIn(MARKER, text)
        self.assertGreater(text.index(MARKER), 3000)

    async def test_catalog_descriptions_are_short_even_for_100_skills(self):
        record_size = 0
        for index in range(100):
            payload = package(name=f"skill-{index}")
            manifest, _, digest = inspect_archive(payload)
            record = NS(
                id=f"id{index}",
                content_hash=digest,
                archive=base64.b64encode(payload).decode(),
                manifest=manifest.model_dump_json(),
                validation_report=None,
            )
            skill = ScriptSkill(record, None, "scripts/a.py")
            self.assertNotIn(MARKER, skill.description)
            self.assertIn("load_skill('custom_skill_%d')" % index, skill.description)
            record_size += len(skill.description)
        self.assertLess(record_size, 100 * 750)
        self.assertGreater(len(manifest.instructions), 15000)

    async def test_bundled_reference_is_readable_by_exact_path_only(self):
        payload = package(files={"references/limits.md": "Limit: 42 alarms per page."})
        await self.add_version("v1", payload)
        registry = await self.registry(self.new_run())
        listing = await self.call(registry, "load_skill", skill_name="custom_alarm_triage")
        self.assertIn("references/limits.md", [f["path"] for f in listing.data["files"]])
        ok = await self.call(
            registry,
            "read_skill_file",
            skill_name="custom_alarm_triage",
            path="references/limits.md",
        )
        self.assertEqual(ok.data["content"], "Limit: 42 alarms per page.")
        for path in (
            "../backend/core/config.py",
            "references/../SKILL.md",
            "/etc/passwd",
            "references\\limits.md",
            "args.json",
            "references/missing.md",
        ):
            with self.subTest(path):
                result = await self.call(
                    registry,
                    "read_skill_file",
                    skill_name="custom_alarm_triage",
                    path=path,
                )
                self.assertFalse(result.success)
        unknown = await self.call(
            registry, "load_skill", skill_name="custom_missing"
        )
        self.assertFalse(unknown.success)

    async def test_run_reads_its_pinned_version_not_the_newly_active_one(self):
        await self.add_version("v1", package(instructions="Version one rules " * 3))
        run = self.new_run()
        await self.registry(run)
        payload = package(instructions="Version two rules " * 3)
        manifest, _, _ = inspect_archive(payload)
        # Same name needs a distinct version; upgrade the packaged version field.
        payload = package(instructions="Version two rules " * 3)
        buffer = BytesIO(payload)
        with zipfile.ZipFile(buffer) as archive:
            files = {n: archive.read(n) for n in archive.namelist()}
        files["SKILL.md"] = files["SKILL.md"].replace(
            b"description: Triage telecom alarm records",
            b"description: Triage telecom alarm records\nmetadata:\n  version: 2.0.0",
        )
        rebuilt = BytesIO()
        with zipfile.ZipFile(rebuilt, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        await self.add_version("v2", rebuilt.getvalue())
        registry = await self.registry(run)
        result = await self.call(registry, "load_skill", skill_name="custom_alarm_triage")
        self.assertIn("Version one", result.data["content"])
        self.assertNotIn("Version two", result.data["content"])

    async def test_revoked_or_changed_version_cannot_be_read(self):
        await self.add_version("v1", package(instructions="Original rules " * 3))
        run = self.new_run()
        registry = await self.registry(run)
        async with self.sessions() as session:
            (await session.get(SkillVersion, "v1")).status = "revoked"
            await session.commit()
        result = await self.call(registry, "load_skill", skill_name="custom_alarm_triage")
        self.assertFalse(result.success)
        self.assertIn("revoked or changed", result.error)
        async with self.sessions() as session:
            record = await session.get(SkillVersion, "v1")
            record.status, record.content_hash = "approved", "0" * 64
            await session.commit()
        result = await self.call(registry, "load_skill", skill_name="custom_alarm_triage")
        self.assertFalse(result.success)

    async def test_builtin_instructions_come_from_the_pinned_document(self):
        base = SkillRegistry()
        base.register(AlarmStatisticsSkill(None, None))
        registry = await self.registry(self.new_run(), base)
        result = await self.call(registry, "load_skill", skill_name="alarm_statistics")
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.data["content"])

    async def test_guide_tools_are_available_to_site_restricted_principals(self):
        await self.add_version("v1", package())
        registry = await self.registry(self.new_run())
        restricted = _principal.set(
            Principal("site-a", frozenset({"operator"}), frozenset({"host-a"}), frozenset())
        )
        self.addCleanup(_principal.reset, restricted)
        result = await self.call(registry, "load_skill", skill_name="custom_alarm_triage")
        self.assertTrue(result.success, result.error)


class SelectedSkillTest(GuideFixture):
    async def test_only_approved_active_or_builtin_skills_are_requestable(self):
        await self.add_version("v1", package(name="active-one"))
        await self.add_version("v2", package(name="pending-one"), status="quarantined", activate=False)
        await self.add_version("v3", package(name="revoked-one"), status="revoked")
        async with self.sessions() as session:
            self.assertTrue(await requestable_skill(session, "custom_active_one"))
            self.assertTrue(await requestable_skill(session, "alarm_statistics"))
            for name in ("custom_pending_one", "custom_revoked_one", "custom_nope"):
                self.assertFalse(await requestable_skill(session, name), name)

    async def test_specific_run_exposes_only_that_package_and_the_readers(self):
        await self.add_version("v1", package(name="alpha-skill"))
        await self.add_version("v2", package(name="beta-skill"))
        base = SkillRegistry()
        base.register(AlarmStatisticsSkill(None, None))
        auto = await self.registry(self.new_run(), base)
        self.assertEqual(
            set(auto.list_names()),
            {"alarm_statistics", "custom_alpha_skill", "custom_beta_skill", "load_skill", "read_skill_file"},
        )
        narrowed = await self.registry(self.new_run("custom_alpha_skill"), base)
        self.assertEqual(
            set(narrowed.list_names()),
            {"custom_alpha_skill", "load_skill", "read_skill_file"},
        )
        other = await self.call(narrowed, "load_skill", skill_name="custom_beta_skill")
        self.assertFalse(other.success)
        with self.assertRaises(ValueError):
            await self.registry(self.new_run("custom_gone"), base)

    async def test_multiscript_package_keeps_every_script_when_selected(self):
        await self.add_version(
            "v1", package(name="multi", scripts=("scripts/a.py", "scripts/b.py"))
        )
        narrowed = await self.registry(self.new_run("custom_multi"))
        tools = [n for n in narrowed.list_names() if n.startswith("custom_multi")]
        self.assertEqual(len(tools), 2)


class FakeProvider:
    def __init__(self, scripts):
        self.scripts, self.index, self.seen_tools = scripts, 0, []

    async def chat_completion(self, messages, tools):
        self.seen_tools.append([t.name for t in tools])
        step = self.scripts[min(self.index, len(self.scripts) - 1)]
        self.index += 1
        calls = [
            NormalizedToolCall(call_id=cid, tool_name=name, arguments=args)
            for cid, name, args in step.get("calls", [])
        ]
        yield NormalizedResponse(text_content=step.get("text"), tool_calls=calls)
        yield NormalizedResponse(stop_reason="tool_calls" if calls else "end_turn")


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    async def execute_call(self, call):
        self.calls.append(call.tool_name)
        from services.skills.base import SkillResult, SkillStatus

        data = (
            {"skill": call.arguments.get("skill_name"), "offset": 0, "content": "rules"}
            if call.tool_name == "load_skill"
            else {"ok": True}
        )
        return SkillResult(status=SkillStatus.SUCCESS, data=data)


class InstructionGateTest(unittest.IsolatedAsyncioTestCase):
    def registry(self):
        from services.skills.base import BaseSkill, SkillResult, SkillStatus

        class Tool(BaseSkill):
            def __init__(self, name):
                self._name = name

            name = property(lambda self: self._name)
            description = property(lambda self: "tool")
            parameters_schema = property(lambda self: {"type": "object", "properties": {}})

            async def execute(self, **kwargs):
                return SkillResult(status=SkillStatus.SUCCESS, data={})

        registry = SkillRegistry()
        for name in ("custom_alpha", "load_skill"):
            registry.register(Tool(name))
        return registry

    async def run_engine(self, scripts, required="custom_alpha"):
        executor, provider = RecordingExecutor(), FakeProvider(scripts)
        engine = AgentEngine(executor, self.registry(), required_skill=required)
        events = [e async for e in engine.run("conv", "check site A", provider, [])]
        return executor, events, provider

    async def test_other_tools_are_blocked_until_instructions_are_loaded(self):
        executor, events, _ = await self.run_engine(
            [
                {"text": "Running", "calls": [("c1", "custom_alpha", {})]},
                {"text": "Loading", "calls": [("c2", "load_skill", {"skill_name": "custom_alpha"})]},
                {"text": "Retry", "calls": [("c3", "custom_alpha", {"n": 1})]},
                {"text": "Done"},
            ]
        )
        self.assertEqual(executor.calls, ["load_skill", "custom_alpha"])
        self.assertEqual(events[-1].type, AgentEventType.FINAL_ANSWER)
        blocked = [
            e for e in events
            if e.type == AgentEventType.TOOL_CALL_RESULT and e.data.get("call_id") == "c1"
        ]
        self.assertTrue(blocked)
        self.assertNotEqual(blocked[0].data["status"], "success")

    async def test_instruction_load_in_the_same_batch_unblocks_later_calls(self):
        executor, events, _ = await self.run_engine(
            [
                {"calls": [("c1", "load_skill", {"skill_name": "custom_alpha"}), ("c2", "custom_alpha", {})]},
                {"text": "Done"},
            ]
        )
        self.assertEqual(executor.calls, ["load_skill", "custom_alpha"])
        self.assertEqual(events[-1].type, AgentEventType.FINAL_ANSWER)

    async def test_loading_a_different_skill_does_not_satisfy_the_gate(self):
        executor, _, _ = await self.run_engine(
            [
                {"calls": [("c1", "load_skill", {"skill_name": "custom_other"}), ("c2", "custom_alpha", {})]},
                {"text": "Done"},
            ]
        )
        self.assertEqual(executor.calls, ["load_skill"])

    async def test_auto_mode_has_no_gate(self):
        executor, _, _ = await self.run_engine(
            [{"calls": [("c1", "custom_alpha", {})]}, {"text": "Done"}], required=None
        )
        self.assertEqual(executor.calls, ["custom_alpha"])

    def test_loads_from_an_earlier_run_do_not_count(self):
        from core.schemas import NormalizedMessage

        def history(user):
            return [
                NormalizedMessage(
                    role="assistant",
                    tool_calls=[NormalizedToolCall(call_id="x", tool_name="load_skill", arguments={"skill_name": "custom_alpha"})],
                ),
                NormalizedMessage(
                    role="tool", tool_call_id="x", tool_name="load_skill",
                    content=json.dumps({"skill": "custom_alpha", "offset": 0}),
                ),
                NormalizedMessage(role="user", content=user),
            ]

        from services.agent_service import _run_start

        messages = history("new request")
        self.assertTrue(instructions_loaded(messages, "custom_alpha"))
        self.assertFalse(
            instructions_loaded(messages[_run_start(messages, "new request"):], "custom_alpha")
        )


if __name__ == "__main__":
    unittest.main()


class RunAdmissionTest(GuideFixture):
    async def create(self, **overrides):
        from services.run_service import create_run

        arguments = dict(
            message="check site A",
            provider="mock",
            conversation_id=None,
            idempotency_key="key",
        )
        async with self.sessions() as session:
            return await create_run(session, **{**arguments, **overrides})

    async def test_unavailable_skill_is_rejected_before_a_run_exists(self):
        from fastapi import HTTPException
        from sqlalchemy import func, select
        from repositories.run_models import Run

        await self.add_version("v1", package(name="pending-one"), status="quarantined", activate=False)
        for name in ("custom_pending_one", "custom_missing"):
            with self.assertRaises(HTTPException) as raised:
                await self.create(skill=name)
            self.assertEqual(raised.exception.status_code, 422)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Run)), 0)

    async def test_selected_skill_is_recorded_and_part_of_idempotency(self):
        from fastapi import HTTPException
        from services.run_service import public_run

        run = await self.create(skill="alarm_statistics")
        self.assertEqual(run.requested_skill, "alarm_statistics")
        self.assertEqual(public_run(run)["skill"], "alarm_statistics")
        repeated = await self.create(skill="alarm_statistics")
        self.assertEqual(repeated.id, run.id)
        with self.assertRaises(HTTPException) as raised:
            await self.create(skill="device_inventory_lookup")
        self.assertEqual(raised.exception.status_code, 409)

    async def test_auto_mode_keeps_the_previous_request_hash(self):
        import hashlib

        run = await self.create()
        self.assertIsNone(run.requested_skill)
        legacy = json.dumps(["check site A", "mock", None], ensure_ascii=False)
        self.assertEqual(run.request_hash, hashlib.sha256(legacy.encode()).hexdigest())
        from services.run_service import public_run

        self.assertEqual(public_run(run)["skill"], "auto")
