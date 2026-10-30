import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from controllers import chat as chat_controller
from repositories.approval_repo import ApprovalRepository
from repositories.conversation_repo import ConversationRepository
from repositories.models import Base, PendingApproval


SUCCESS = {"executed": True, "command_succeeded": True, "exit_code": 0, "stdout": "ok", "stderr": ""}


class ApprovalConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    use_postgres = False

    async def asyncSetUp(self):
        if self.use_postgres:
            url = make_url(os.environ["APPROVAL_TEST_POSTGRES_URL"])
            if (
                url.drivername != "postgresql+asyncpg"
                or url.database != "noc_agent_approval_test"
                or url.host not in {"127.0.0.1", "localhost"}
            ):
                raise ValueError("Use a local, disposable noc_agent_approval_test database")
            schema = f"approval_test_{uuid4().hex}"
            admin_engine = create_async_engine(url)
            self.addAsyncCleanup(admin_engine.dispose)
            async with admin_engine.begin() as connection:
                await connection.execute(CreateSchema(schema))

            async def drop_test_schema():
                async with admin_engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))

            self.addAsyncCleanup(drop_test_schema)
            self.db_engine = create_async_engine(
                url, connect_args={"server_settings": {"search_path": schema}},
            )
        else:
            temp_dir = tempfile.TemporaryDirectory()
            self.addCleanup(temp_dir.cleanup)
            db_path = Path(temp_dir.name, "approvals.db").as_posix()
            self.db_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.addAsyncCleanup(self.db_engine.dispose)
        async with self.db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.db_engine, expire_on_commit=False)
        self.enterContext(patch.object(chat_controller, "async_session", self.session_factory))
        self.enterContext(patch.object(chat_controller, "engine", object()))
        self.execute = self.enterContext(patch.object(
            chat_controller.NodeHealthRemediateSkill, "execute_approved_command", return_value=SUCCESS,
        ))
        async with self.session_factory() as session:
            conversation = await ConversationRepository(session).create("mock")
            self.conversation_id = conversation.id
            await ApprovalRepository(session).create_pending(
                conversation_id=self.conversation_id,
                call_id="call-restart",
                tool_name="node_health_remediate",
                arguments={"host": "test-node", "dry_run": False},
                approval_metadata={
                    "host": "test-node",
                    "command": "docker restart test-service",
                    "action": "restart_service",
                },
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            )

    async def _decide(self, approved=True):
        response = await chat_controller.approve_action(chat_controller.ApprovalDecisionRequest(
            conversation_id=self.conversation_id, call_id="call-restart", approved=approved,
        ))
        return [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]

    async def _state(self):
        async with self.session_factory() as session:
            row = await ApprovalRepository(session).get(self.conversation_id, "call-restart")
            messages = await ConversationRepository(session).get_messages(self.conversation_id)
            return row, messages

    def _synchronize_pending_reads(self):
        """Make both requests observe pending before either can attempt its claim."""
        original_get = ApprovalRepository.get
        both_read = asyncio.Event()
        reads = 0

        async def get(repo, conversation_id, call_id):
            nonlocal reads
            row = await original_get(repo, conversation_id, call_id)
            if row is not None and row.status == "pending" and reads < 2:
                reads += 1
                if reads == 2:
                    both_read.set()
                await asyncio.wait_for(both_read.wait(), timeout=10)
            return row

        return patch.object(ApprovalRepository, "get", get)

    async def _assert_race(self, decisions):
        with self._synchronize_pending_reads():
            outcomes = await asyncio.wait_for(
                asyncio.gather(*(self._decide(decision) for decision in decisions), return_exceptions=True),
                timeout=15,
            )
        successes = [outcome for outcome in outcomes if isinstance(outcome, list)]
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual([outcome.status_code for outcome in conflicts], [409], outcomes)
        row, messages = await self._state()
        self.assertIn(row.status, {"approved", "rejected"})
        self.assertEqual(self.execute.call_count, int(row.status == "approved"))
        self.assertEqual([message["role"] for message in messages], ["tool", "assistant"])
        return row

    async def test_simultaneous_approvals_execute_only_once(self):
        row = await self._assert_race([True, True])
        self.assertEqual(row.status, "approved")
        self.execute.assert_called_once_with(host="test-node", command="docker restart test-service")

    async def test_simultaneous_approve_and_reject_have_only_one_winner(self):
        await self._assert_race([True, False])

    async def test_simultaneous_rejections_write_only_one_outcome(self):
        row = await self._assert_race([False, False])
        self.assertEqual(row.status, "rejected")
        self.execute.assert_not_called()

    async def test_approved_retry_replays_saved_result_without_execution_or_messages(self):
        first = await self._decide()
        replay = await self._decide()
        self.assertEqual(first[0]["data"], replay[0]["data"])
        self.assertEqual(self.execute.call_count, 1)
        row, messages = await self._state()
        self.assertEqual(row.status, "approved")
        self.assertEqual(len(messages), 2)

    async def test_rejected_retry_replays_saved_result_without_execution_or_messages(self):
        first = await self._decide(False)
        replay = await self._decide(False)
        self.assertEqual(first[0]["data"], replay[0]["data"])
        self.execute.assert_not_called()
        row, messages = await self._state()
        self.assertEqual(row.status, "rejected")
        self.assertEqual(len(messages), 2)

    async def test_opposite_decision_cannot_override_completed_approval(self):
        await self._decide(False)
        with self.assertRaises(HTTPException) as raised:
            await self._decide(True)
        self.assertEqual(raised.exception.status_code, 409)
        row, messages = await self._state()
        self.assertEqual(row.status, "rejected")
        self.assertEqual(len(messages), 2)
        self.execute.assert_not_called()

    async def test_expired_approval_is_not_executed(self):
        async with self.session_factory() as session:
            await session.execute(update(PendingApproval).values(
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
            ))
            await session.commit()
        with self.assertRaises(HTTPException) as raised:
            await self._decide()
        self.assertEqual(raised.exception.status_code, 409)
        row, _ = await self._state()
        self.assertEqual(row.status, "expired")
        self.execute.assert_not_called()

    async def test_claim_rechecks_expiry_after_the_request_reads_pending(self):
        original_get = ApprovalRepository.get

        async def expire_after_read(repo, conversation_id, call_id):
            row = await original_get(repo, conversation_id, call_id)
            async with self.session_factory() as other_session:
                await other_session.execute(update(PendingApproval).where(
                    PendingApproval.id == row.id,
                ).values(expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)))
                await other_session.commit()
            return row

        with patch.object(ApprovalRepository, "get", expire_after_read):
            with self.assertRaises(HTTPException) as raised:
                await self._decide()
        self.assertEqual(raised.exception.status_code, 409)
        self.execute.assert_not_called()

    async def test_changed_action_snapshot_cannot_execute_the_stale_command(self):
        original_get = ApprovalRepository.get

        async def change_after_read(repo, conversation_id, call_id):
            row = await original_get(repo, conversation_id, call_id)
            async with self.session_factory() as other_session:
                await other_session.execute(update(PendingApproval).where(
                    PendingApproval.id == row.id,
                ).values(approval_metadata=json.dumps({"host": "other-node", "command": "changed"})))
                await other_session.commit()
            return row

        with patch.object(ApprovalRepository, "get", change_after_read):
            with self.assertRaises(HTTPException) as raised:
                await self._decide()
        self.assertEqual(raised.exception.status_code, 409)
        self.execute.assert_not_called()
        row, messages = await self._state()
        self.assertEqual(row.status, "pending")
        self.assertEqual(messages, [])

    async def test_uncertain_ssh_result_requires_reconciliation_and_cannot_retry(self):
        self.execute.return_value = {
            "executed": False, "command_succeeded": False, "exit_code": None, "stderr": "Connection lost",
        }
        events = await self._decide()
        self.assertEqual(events[0]["data"]["status"], "system_error")
        row, messages = await self._state()
        self.assertEqual(row.status, "unknown")
        self.assertIsNone(json.loads(row.result)["executed"])
        self.assertIn("đối soát", messages[-1]["content"])
        with self.assertRaises(HTTPException) as raised:
            await self._decide()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.execute.call_count, 1)

    async def test_executor_exception_is_recorded_as_unknown(self):
        self.execute.side_effect = OSError("Connection interrupted after dispatch")
        with self.assertLogs(chat_controller.logger, level="ERROR"):
            await self._decide()
        row, messages = await self._state()
        self.assertEqual(row.status, "unknown")
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0]["is_error"])

    async def test_nonzero_exit_is_known_failure_and_does_not_rerun(self):
        self.execute.return_value = {**SUCCESS, "command_succeeded": False, "exit_code": 1, "stderr": "failed"}
        await self._decide()
        await self._decide()
        row, messages = await self._state()
        self.assertEqual(row.status, "approved")
        self.assertEqual(json.loads(row.result)["exit_code"], 1)
        self.assertTrue(messages[0]["is_error"])
        self.assertEqual(self.execute.call_count, 1)

    async def test_persistence_failure_keeps_durable_claim_and_prevents_retry(self):
        with (
            patch.object(ConversationRepository, "add_messages", side_effect=RuntimeError("DB unavailable")),
            self.assertLogs(chat_controller.logger, level="ERROR"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await self._decide()
        self.assertEqual(raised.exception.status_code, 503)
        row, messages = await self._state()
        self.assertEqual(row.status, "executing")
        self.assertIsNone(row.result)
        self.assertEqual(messages, [])
        with self.assertRaises(HTTPException) as raised:
            await self._decide()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.execute.call_count, 1)

    async def test_cancelled_request_does_not_allow_a_second_command(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()

        def blocked_command(**kwargs):
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(timeout=15):
                    raise TimeoutError("Test command was not released")
                return SUCCESS
            finally:
                loop.call_soon_threadsafe(finished.set)

        self.execute.side_effect = blocked_command
        task = asyncio.create_task(self._decide())
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            row, _ = await self._state()
            self.assertEqual(row.status, "executing")
            with self.assertRaises(HTTPException) as raised:
                await self._decide()
            self.assertEqual(raised.exception.status_code, 409)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            row, messages = await self._state()
            self.assertEqual(row.status, "unknown")
            self.assertEqual(len(messages), 2)
            with self.assertRaises(HTTPException):
                await self._decide()
            self.assertEqual(self.execute.call_count, 1)
        finally:
            release.set()
            if not task.done():
                await task
            await asyncio.wait_for(finished.wait(), timeout=10)

    async def test_unclaimed_approval_cannot_be_marked_completed(self):
        async with self.session_factory() as session:
            repo = ApprovalRepository(session)
            row = await repo.get(self.conversation_id, "call-restart")
            self.assertFalse(await repo.resolve(row, "approved", result=SUCCESS))
        row, _ = await self._state()
        self.assertEqual(row.status, "pending")


@unittest.skipUnless(os.environ.get("APPROVAL_TEST_POSTGRES_URL"), "Disposable PostgreSQL test URL not configured")
class PostgresApprovalConcurrencyTest(ApprovalConcurrencyTest):
    use_postgres = True


if __name__ == "__main__":
    unittest.main()
