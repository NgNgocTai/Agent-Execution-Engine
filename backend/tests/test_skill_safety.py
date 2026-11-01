import base64
from io import BytesIO
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from core.auth import Principal, _principal
from core.config import settings
from controllers import skills
from repositories.models import Base
from repositories.skill_models import SkillVersion
from services.skill_validation import inspect_archive
from services.sandbox_executor import execute_sandbox, SandboxUnavailable
from services.skills.base import SkillResult, SkillStatus


MANIFEST = {"name": "custom_transform", "version": "1.0.0", "description": "Transform structured alarm input", "entrypoint": "main.py", "input_fields": [{"name": "text", "kind": "string"}], "sample_input": {"text": "hello"}}
CODE = 'import json, sys\nargs = json.load(sys.stdin)\nprint(json.dumps({"status": "success", "data": {"text": args["text"].upper()}}))\n'


def archive(extra=None, manifest=None, code=CODE):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zip:
        zip.writestr("manifest.json", json.dumps(manifest or MANIFEST))
        zip.writestr("main.py", code)
        for name, data in extra or []:
            zip.writestr(name, data)
    return buffer.getvalue()


class ArchiveSafetyTest(unittest.TestCase):
    def test_safe_archive_has_strict_declared_inputs(self):
        manifest, files, digest = inspect_archive(archive())
        self.assertEqual(len(digest), 64)
        self.assertIn("main.py", files)
        with self.assertRaises(ValueError):
            manifest.input_model().model_validate({"text": "hello", "unknown": True})
    def test_unsafe_paths_and_collisions_are_rejected(self):
        for name in ("../escape.py", "/absolute.py", "C:/absolute.py", "a\\evil.py", "./alias.py", "folder/../evil.py", "MAIN.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                payload = archive([(name, "pass")])
                # Windows ZipInfo normalizes its native separator when writing;
                # craft the raw member name that an external uploader can send.
                if "\\" in name:
                    payload = payload.replace(name.replace("\\", "/").encode(), name.encode())
                inspect_archive(payload)
    def test_links_bombs_binary_files_and_extra_manifest_fields_are_rejected(self):
        link = zipfile.ZipInfo("link.py"); link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [archive([("evil.exe", "binary")]), archive([("bomb.txt", "x" * 1000000)]),
                 archive([(link, "main.py")]), archive(manifest={**MANIFEST, "network": "host"}),
                 archive(manifest={**MANIFEST, "name": "node_health_remediate"})]
        for payload in cases:
            with self.assertRaises(ValueError): inspect_archive(payload)
    def test_static_scan_is_not_an_execution_path(self):
        for code in ("import subprocess", "open('/etc/passwd')", "eval('1')", "password='fixture-secret'"):
            with self.assertRaises(ValueError): inspect_archive(archive(code=code))


class CatalogSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.db = create_async_engine("sqlite+aiosqlite:///" + Path(self.directory.name, "skills.db").as_posix())
        self.addAsyncCleanup(self.db.dispose)
        async with self.db.begin() as connection: await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db, expire_on_commit=False)
        self.enterContext(patch.object(skills, "async_session", self.sessions))
        self.context = _principal.set(Principal("reviewer", frozenset({"skill_reviewer"})))
        self.addCleanup(_principal.reset, self.context)
        payload = archive()
        manifest, _, digest = inspect_archive(payload)
        self.id, self.digest = uuid4().hex, digest
        async with self.sessions() as session:
            session.add(SkillVersion(id=self.id, name=manifest.name, version=manifest.version, content_hash=digest,
                manifest=manifest.model_dump_json(), archive=base64.b64encode(payload).decode(), status="quarantined", uploaded_by="uploader"))
            await session.commit()

    async def test_sandbox_unavailable_never_approves_or_runs_host_code(self):
        with patch.object(skills, "execute_sandbox", side_effect=SandboxUnavailable("offline")):
            with self.assertRaises(HTTPException) as raised:
                await skills.review(self.id, skills.Review(content_hash=self.digest, approved=True, notes="Reviewed source carefully"))
        self.assertEqual(raised.exception.status_code, 503)
        async with self.sessions() as session:
            self.assertEqual((await session.get(SkillVersion, self.id)).status, "quarantined")

    async def test_self_review_and_stale_hash_are_denied(self):
        context = _principal.set(Principal("uploader", frozenset({"skill_reviewer"})))
        try:
            with self.assertRaises(HTTPException) as raised:
                await skills.review(self.id, skills.Review(content_hash=self.digest, approved=True, notes="Reviewed source carefully"))
            self.assertEqual(raised.exception.status_code, 403)
        finally: _principal.reset(context)
        with self.assertRaises(HTTPException) as raised:
            await skills.review(self.id, skills.Review(content_hash="0" * 64, approved=True, notes="Reviewed source carefully"))
        self.assertEqual(raised.exception.status_code, 409)

    async def test_no_sandbox_digest_denies_execution_before_starting_a_process(self):
        manifest, files, _ = inspect_archive(archive())
        with patch.object(settings, "sandbox_image", "python:latest"), patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as process:
            with self.assertRaises(SandboxUnavailable): await execute_sandbox(manifest, files, {"text": "hello"})
            process.assert_not_called()

    async def test_successful_review_is_bound_to_the_hash_and_cannot_be_repeated(self):
        with patch.object(skills, "execute_sandbox", return_value=SkillResult(status=SkillStatus.SUCCESS, data={"ok": True})):
            result = await skills.review(self.id, skills.Review(content_hash=self.digest, approved=True, notes="Reviewed source and transformation permissions"))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["reviewed_by"], "reviewer")
        with self.assertRaises(HTTPException) as raised:
            await skills.review(self.id, skills.Review(content_hash=self.digest, approved=True, notes="Repeated review is not allowed"))
        self.assertEqual(raised.exception.status_code, 409)

    async def test_revoked_pinned_version_cannot_execute(self):
        from services.skill_catalog import ScriptSkill
        import services.skill_catalog as catalog
        async with self.sessions() as session:
            record = await session.get(SkillVersion, self.id)
            record.status = "approved"
            await session.commit()
            pinned = ScriptSkill(record, self.sessions)
            record.status = "revoked"
            await session.commit()
        with patch.object(catalog, "execute_sandbox", new_callable=AsyncMock) as sandbox:
            with self.assertRaises(ValueError): await pinned.execute(text="hello")
            sandbox.assert_not_called()
