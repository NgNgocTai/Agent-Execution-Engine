import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from controllers import runs as routes, chat
from core.config import settings
from core.schemas import NormalizedResponse, NormalizedToolCall
from repositories.models import Base, PendingApproval
from repositories.run_models import Run, RunJob, RunEvent, RunCheckpoint, ToolExecution
from services.agent_service import AgentEngine
from services.job_service import claim, fence, LeaseLost, recover_expired
from services.run_worker import RunWorker
from services.run_service import create_run, cancel_run
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor


class RunTest(unittest.IsolatedAsyncioTestCase):
    use_postgres = False
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        if self.use_postgres:
            from postgres_fixture import postgres_database
            self.db = await self.enterAsyncContext(postgres_database())
        else:
            self.db = create_async_engine("sqlite+aiosqlite:///" + Path(self.directory.name, "runs.db").as_posix())
            self.addAsyncCleanup(self.db.dispose)
        async with self.db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db, expire_on_commit=False)
        self.registry = SkillRegistry()
        self.engine = AgentEngine(ToolExecutor(self.registry), self.registry)
        self.enterContext(patch.object(routes, "async_session", self.sessions))
        self.enterContext(patch.object(chat, "async_session", self.sessions))
        self.enterContext(patch.object(chat, "engine", self.engine))
        self.enterContext(patch.object(settings, "ssh_allowed_hosts", "node-a"))
        self.enterContext(patch.object(settings, "ssh_allowed_services", "api"))

    async def create(self, key="test-key", conversation_id=None):
        async with self.sessions() as session:
            return await create_run(session, message="hello", provider="mock", conversation_id=conversation_id, idempotency_key=key)

    async def test_idempotency_and_active_run_conflicts(self):
        first = await self.create()
        repeated = await self.create()
        self.assertEqual(first.id, repeated.id)
        with self.assertRaises(HTTPException) as raised:
            await self.create("other", first.conversation_id)
        self.assertEqual(raised.exception.status_code, 409)
        async with self.sessions() as session:
            with self.assertRaises(HTTPException):
                await create_run(session, message="different", provider="mock", conversation_id=None, idempotency_key="test-key")

    async def test_two_workers_cannot_claim_same_job(self):
        await self.create()
        async def attempt(name):
            async with self.sessions() as session:
                return await claim(session, name)
        results = await asyncio.gather(attempt("one"), attempt("two"))
        self.assertEqual(sum(result is not None for result in results), 1)

    async def test_actor_admission_limit_is_atomic(self):
        with patch.object(settings, "max_active_runs_per_actor", 1):
            results = await asyncio.gather(self.create("first"), self.create("second"), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, Run) for result in results), 1)
        errors = [result for result in results if isinstance(result, HTTPException)]
        self.assertEqual([error.status_code for error in errors], [429])

    async def test_cancellation_fences_old_worker(self):
        run = await self.create()
        async with self.sessions() as session:
            _, token = await claim(session, "worker")
            await cancel_run(session, run.id)
        async with self.sessions() as session:
            with self.assertRaises(LeaseLost):
                await fence(session, run.id, token)

    async def test_waiting_checkpoint_version_mismatch_blocks_resume_explicitly(self):
        from services.approval_resume import queue_resolved_approval

        run = await self.create()
        async with self.sessions() as session:
            await session.execute(update(Run).where(Run.id == run.id).values(status="waiting_approval"))
            await session.execute(update(RunJob).where(RunJob.run_id == run.id).values(status="waiting_approval"))
            session.add(RunCheckpoint(run_id=run.id, version=99, state="{}"))
            await session.commit()
            self.assertTrue(await queue_resolved_approval(session, run.id))
        snapshot = await routes.snapshot(run.id)
        self.assertEqual(snapshot["status"], "unknown")
        self.assertEqual(snapshot["error_code"], "CHECKPOINT_INCOMPATIBLE")
        async with self.sessions() as session:
            self.assertIsNone(await claim(session, "new-worker"))

    async def test_worker_commits_final_messages_and_replay_survives_detach(self):
        class Provider:
            async def chat_completion(self, messages, tools):
                yield NormalizedResponse(text_content="Final answer")
        run = await self.create()
        async with self.sessions() as session:
            job = await claim(session, "worker")
        await RunWorker(self.engine, self.sessions, lambda name: Provider()).process(*job)
        snapshot = await routes.snapshot(run.id)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["messages"][-1]["content"], "Final answer")
        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.get(f"/api/runs/{run.id}/events?after=0")
            replay = await client.get(f"/api/runs/{run.id}/events?after=1")
            self.assertEqual(first.status_code, 200)
            self.assertIn("id: 1", first.text)
            self.assertNotIn("id: 1\n", replay.text)
            ahead = await client.get(f"/api/runs/{run.id}/events?after=999")
            self.assertEqual(ahead.status_code, 409)

    async def test_expired_write_is_unknown_and_not_requeued(self):
        run = await self.create()
        async with self.sessions() as session:
            await claim(session, "dead-worker")
            await session.execute(update(RunJob).where(RunJob.run_id == run.id).values(lease_until=datetime.utcnow() - timedelta(seconds=1)))
            session.add(ToolExecution(id="execution", run_id=run.id, call_id="call", tool_name="write", arguments="{}", status="running", is_write=True))
            await session.commit()
            await recover_expired(session)
            saved = await session.get(Run, run.id)
            self.assertEqual(saved.status, "unknown")
            self.assertIsNone(await claim(session, "new-worker"))

    async def test_resume_keeps_other_calls_in_pending_batch(self):
        class Skill(BaseSkill):
            name = "write_fixture"
            description = "Fixture"
            parameters_schema = {"type": "object"}
            async def execute(self, **kwargs):
                return SkillResult(status=SkillStatus.SUCCESS, data={}, needs_approval=True,
                    approval_metadata={"host": "node-a", "service": "api", "action": "restart_service", "command": "docker restart api"})
        class Provider:
            async def chat_completion(self, messages, tools):
                if any(message.role == "tool" for message in messages):
                    yield NormalizedResponse(text_content="Completed all calls")
                else:
                    yield NormalizedResponse(tool_calls=[NormalizedToolCall(call_id="one", tool_name="node_health_remediate", arguments={"host": "node-a", "service": "api", "dry_run": False}),
                        NormalizedToolCall(call_id="two", tool_name="read_fixture", arguments={})])
        class Read(Skill):
            name = "read_fixture"
            async def execute(self, **kwargs):
                return SkillResult(status=SkillStatus.SUCCESS, data={"read": True})
        skill = Skill()
        skill.name = "node_health_remediate"
        self.registry.register(skill)
        self.registry.register(Read())
        run = await self.create()
        worker = RunWorker(self.engine, self.sessions, lambda name: Provider())
        async with self.sessions() as session:
            job = await claim(session, "worker")
        await worker.process(*job)
        self.assertEqual((await routes.snapshot(run.id))["status"], "waiting_approval")
        await routes.intervene(run.id, routes.InterventionRequest(message="Include a clear follow-up recommendation"))
        async with self.sessions() as session:
            original = await session.scalar(select(PendingApproval).where(PendingApproval.conversation_id == run.conversation_id))
            self.assertEqual(json.loads(original.approval_metadata)["command"], "docker restart api")
        with patch.object(chat.NodeHealthRemediateSkill, "execute_approved_command", return_value={"executed": True, "command_succeeded": True, "exit_code": 0}):
            # The fixture name is deliberately the supported dispatcher name.
            await routes.approve(run.id, routes.RunApprovalRequest(call_id="one", approved=True))
        async with self.sessions() as session:
            job = await claim(session, "worker")
        await worker.process(*job)
        snapshot = await routes.snapshot(run.id)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["messages"][-1]["content"], "Completed all calls")
        async with self.sessions() as session:
            executions = (await session.scalars(select(ToolExecution).where(ToolExecution.run_id == run.id))).all()
            self.assertEqual({execution.call_id for execution in executions}, {"one", "two"})
        self.assertTrue(any("Operator intervention" in (message.get("content") or "") for message in snapshot["messages"]))


@unittest.skipUnless(os.environ.get("APPROVAL_TEST_POSTGRES_URL"), "Disposable PostgreSQL URL is not configured")
class PostgresRunTest(RunTest):
    use_postgres = True
