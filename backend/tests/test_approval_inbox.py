from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from controllers import runs as routes
from core.auth import Principal, _principal
from core.clock import utcnow
from core.config import settings
from repositories.models import Base, Conversation, PendingApproval
from repositories.run_models import Run


def who(subject, *roles, hosts=("*",), services=("*",)):
    return Principal(subject, frozenset(roles), frozenset(hosts), frozenset(services), frozenset())


class ApprovalInboxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = create_async_engine("sqlite+aiosqlite:///" + Path(directory.name, "inbox.db").as_posix())
        self.addAsyncCleanup(self.db.dispose)
        async with self.db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db, expire_on_commit=False)
        self.enterContext(patch.object(routes, "async_session", self.sessions))
        self.enterContext(patch.object(settings, "ssh_allowed_hosts", "node-a,node-b"))
        self.enterContext(patch.object(settings, "ssh_allowed_services", "api"))
        now = utcnow()
        async with self.sessions() as session:
            for cid, owner, deleted in (("conv-a", "alice", None), ("conv-a2", "alice", None), ("conv-b", "bob", None), ("conv-gone", "alice", now)):
                session.add(Conversation(id=cid, provider="mock", owner_id=owner, title=f"title {cid}", deleted_at=deleted))
            await session.flush()

            def approval(cid, call, host, minutes, status="pending", command="docker restart api"):
                session.add(PendingApproval(
                    conversation_id=cid, call_id=call, tool_name="node_health_remediate",
                    arguments=json.dumps({"host": host}), status=status, expires_at=now + timedelta(minutes=minutes),
                    approval_metadata=json.dumps({"host": host, "service": "api", "action": "restart_service", "command": command}),
                ))

            approval("conv-a", "late", "node-a", 9)
            approval("conv-a2", "soon", "node-b", 2, command="docker restart api password=hunter2-secret")
            approval("conv-a", "expired", "node-a", -1)
            approval("conv-a", "done", "node-a", 5, status="approved")
            approval("conv-b", "bobs", "node-a", 3)
            approval("conv-gone", "deleted", "node-a", 4)
            session.add(Run(id="run-a2", conversation_id="conv-a2", actor="alice", provider="mock", user_message="m",
                            status="waiting_approval", idempotency_key="k", request_hash="h", sequence=1))
            await session.commit()

    async def inbox(self, principal):
        token = _principal.set(principal)
        try:
            return (await routes.approval_inbox(limit=100))["approvals"]
        finally:
            _principal.reset(token)

    async def test_owner_sees_only_live_pending_approvals_of_their_conversations_soonest_first(self):
        items = await self.inbox(who("alice", "approver"))
        self.assertEqual([i["call_id"] for i in items], ["soon", "late"])
        self.assertEqual(items[0]["conversation_title"], "title conv-a2")
        self.assertEqual(items[0]["run_id"], "run-a2")
        self.assertIsNone(items[1]["run_id"])

    async def test_admin_sees_every_owner_but_never_expired_decided_or_deleted(self):
        items = await self.inbox(who("root", "admin"))
        self.assertEqual([i["call_id"] for i in items], ["soon", "bobs", "late"])

    async def test_actionable_reflects_the_callers_host_scope_and_the_server_allowlist(self):
        items = {i["call_id"]: i for i in await self.inbox(who("alice", "approver", hosts=("node-a",), services=("api",)))}
        self.assertTrue(items["late"]["actionable"])
        self.assertFalse(items["soon"]["actionable"])  # node-b is outside alice's scope
        with patch.object(settings, "ssh_allowed_hosts", "node-b"):
            items = {i["call_id"]: i for i in await self.inbox(who("alice", "approver"))}
        self.assertFalse(items["late"]["actionable"])  # server no longer allows node-a

    async def test_secrets_in_approval_details_are_redacted(self):
        items = await self.inbox(who("alice", "approver"))
        self.assertNotIn("hunter2-secret", json.dumps(items))

    async def test_only_approvers_and_admins_can_open_the_inbox(self):
        for principal in (who("alice", "viewer"), who("alice", "operator")):
            with self.subTest(principal.roles), self.assertRaises(HTTPException) as raised:
                await self.inbox(principal)
            self.assertEqual(raised.exception.status_code, 403)

    async def test_route_is_not_captured_by_the_legacy_approval_route(self):
        import httpx
        from fastapi import FastAPI
        from controllers import chat

        app = FastAPI()
        app.include_router(chat.router)
        app.include_router(routes.router)
        token = _principal.set(who("alice", "approver"))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                response = await client.get("/api/approvals/pending")
        finally:
            _principal.reset(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["approvals"]), 2)


if __name__ == "__main__":
    unittest.main()
