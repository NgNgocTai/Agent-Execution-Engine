import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from core.auth import AuthenticationMiddleware, Principal, _principal, authenticate, require
from core.config import settings
from core.policy import Decision, approved_snapshot, plan
from core.redaction import redact
from core.schemas import AgentEvent, AgentEventType
from repositories.models import Base
from repositories.conversation_repo import ConversationRepository
from services.skills.node_health_remediate import NodeHealthRemediateSkill
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor


class SecurityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(settings, "ssh_allowed_hosts", "node-a"))
        self.enterContext(patch.object(settings, "ssh_allowed_services", "api"))

    async def test_invalid_input_never_reaches_connector(self):
        skill = NodeHealthRemediateSkill()
        registry = SkillRegistry()
        registry.register(skill)
        with patch.object(skill, "_get_metrics") as metrics:
            for arguments in ({"host": "node-a", "dry_run": "false"}, {"host": "node-a", "extra": 1},
                              {"host": "node-a", "cpu_threshold": 101}, {"host": "node-a", "log_lines": -1},
                              {"host": "node-a", "service": "api; id"}, {"host": "node-b"}):
                result = await ToolExecutor(registry).execute(skill.name, arguments)
                self.assertFalse(result.success)
            metrics.assert_not_called()

    async def test_approved_command_must_match_current_allowlist_and_snapshot(self):
        snapshot = {"host": "node-a", "service": "api", "action": "restart_service", "command": "docker restart api"}
        self.assertTrue(approved_snapshot(snapshot))
        for key, value in (("host", "node-b"), ("service", "database"), ("command", "docker restart api; id")):
            self.assertFalse(approved_snapshot({**snapshot, key: value}))
        with patch.object(settings, "ssh_allowed_hosts", ""):
            self.assertFalse(approved_snapshot(snapshot))

    async def test_authenticated_scope_and_role_are_enforced(self):
        context = _principal.set(Principal("alice", frozenset({"operator"}), frozenset({"node-a"}), frozenset({"api"})))
        try:
            self.assertEqual(plan("node_health_remediate", {"host": "node-b"}), Decision.DENY)
            self.assertEqual(plan("node_health_remediate", {"host": "node-a", "service": "api", "dry_run": False}), Decision.REQUIRE_APPROVAL)
            with self.assertRaises(HTTPException):
                require("approver")
        finally:
            _principal.reset(context)

    async def test_cross_user_conversation_access_is_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine("sqlite+aiosqlite:///" + Path(directory, "security.db").as_posix())
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    repo = ConversationRepository(session)
                    context = _principal.set(Principal("alice", frozenset({"operator"})))
                    conversation = await repo.create("mock")
                    _principal.reset(context)
                    context = _principal.set(Principal("bob", frozenset({"operator"})))
                    try:
                        self.assertIsNone(await repo.get(conversation.id))
                        self.assertEqual(await repo.list_all(), [])
                        self.assertFalse(await repo.rename(conversation.id, "stolen"))
                        self.assertFalse(await repo.delete(conversation.id))
                    finally:
                        _principal.reset(context)
            finally:
                await engine.dispose()

    async def test_bearer_middleware_ignores_forged_identity_headers(self):
        token = "test-token-with-enough-entropy-for-fixture"
        entries = json.dumps([{"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "subject": "alice", "roles": ["viewer"]}])
        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware)

        @app.get("/api/me")
        async def me():
            return {"subject": require("viewer").subject}

        with patch.object(settings, "auth_mode", "bearer"), patch.object(settings, "auth_principals_json", entries):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/api/me", headers={"X-User": "admin"})).status_code, 401)
                response = await client.get("/api/me", headers={"Authorization": "Bearer " + token, "X-User": "bob"})
                self.assertEqual(response.json(), {"subject": "alice"})
            with self.assertRaises(HTTPException):
                authenticate("wrong")

    def test_redaction_preserves_original_and_removes_nested_secrets(self):
        original = {"password": "sentinel", "data": "password=hidden Bearer abc.def postgresql://user:pass@host/db", "token_usage": {"input": 10}}
        safe = json.dumps(redact(original))
        for secret in ("sentinel", "hidden", "abc.def", "user:pass"):
            self.assertNotIn(secret, safe)
        self.assertEqual(original["password"], "sentinel")
        self.assertIn('"input": 10', safe)
        event = AgentEvent(type=AgentEventType.TEXT_DELTA, data={"content": "api_key=secret"})
        self.assertNotIn("=secret", event.to_sse())
        quoted = redact('password="a multi word secret" id_token=abc123')
        self.assertNotIn("multi word secret", quoted)
        self.assertNotIn("abc123", quoted)
        self.assertNotIn("jwt-value", json.dumps(redact({"id_token": "jwt-value"})))
