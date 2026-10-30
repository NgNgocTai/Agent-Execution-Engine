import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from controllers import chat as chat_controller
from core.schemas import AgentEventType, NormalizedResponse, NormalizedToolCall
from repositories.approval_repo import ApprovalRepository
from repositories.conversation_repo import ConversationRepository
from repositories.models import Base, Message
from services.agent_service import AgentEngine
from services.skills.base import SkillResult, SkillStatus


class ScriptedProvider:
    model_name = "persistence-test"

    def __init__(self, use_tool=False):
        self.use_tool = use_tool
        self.calls = 0

    async def chat_completion(self, messages, tools, stream=True):
        self.calls += 1
        if self.use_tool and self.calls == 1:
            yield NormalizedResponse(tool_calls=[NormalizedToolCall(
                call_id="call-inventory",
                tool_name="device_inventory_lookup",
                arguments={"device_name": "test-node"},
            )])
        else:
            yield NormalizedResponse(text_content="Inventory checked.")


class FakeRegistry:
    def get_tool_schemas(self):
        return []


class FakeExecutor:
    def __init__(self):
        self.needs_approval = False

    async def execute(self, tool_name, arguments):
        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={"device_name": "test-node"},
            needs_approval=self.needs_approval,
            approval_metadata={
                "host": "test-node",
                "action": "restart_service",
                "command": "docker restart test-service",
            } if self.needs_approval else None,
        )


class ChatPersistenceTest(unittest.IsolatedAsyncioTestCase):
    """Exercise real engine/controller persistence against an isolated SQLite DB."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = Path(self.temp_dir.name, "chat.db").as_posix()
        self.db_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.addAsyncCleanup(self.db_engine.dispose)
        self.session_factory = async_sessionmaker(self.db_engine, expire_on_commit=False)
        async with self.db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.active_sessions = set()
        self.opened_sessions = 0
        self.fail_stream_commits = False

        @asynccontextmanager
        async def tracked_session():
            self.opened_sessions += 1
            async with self.session_factory() as session:
                self.active_sessions.add(id(session))
                try:
                    if self.fail_stream_commits:
                        with patch.object(session, "commit", side_effect=RuntimeError("DB unavailable")):
                            yield session
                    else:
                        yield session
                finally:
                    self.active_sessions.remove(id(session))

        self.executor = FakeExecutor()
        self.agent = AgentEngine(self.executor, FakeRegistry(), tracer=None)
        self.enterContext(patch.object(chat_controller, "async_session", tracked_session))
        self.enterContext(patch.object(chat_controller, "engine", self.agent))

    async def _seed_conversation(self, history_length=0):
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"old-{index}"}
            for index in range(history_length)
        ]
        async with self.session_factory() as session:
            conversation = await ConversationRepository(session).create(provider="mock")
            session.add_all([
                Message(conversation_id=conversation.id, **message) for message in history
            ])
            await session.commit()
            return conversation.id, history

    async def _read_messages(self, conversation_id):
        async with self.session_factory() as session:
            return await ConversationRepository(session).get_messages(conversation_id)

    async def _start_chat(self, conversation_id, use_tool=False):
        provider = ScriptedProvider(use_tool=use_tool)
        with patch.object(chat_controller, "_get_provider", return_value=provider):
            return await chat_controller.chat(chat_controller.ChatRequest(
                conversation_id=conversation_id,
                message="Check inventory",
                provider="mock",
            ))

    @staticmethod
    def _event(chunk):
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        return json.loads(chunk.removeprefix("data: ").strip())

    async def test_new_turn_survives_history_truncation_without_duplicating_old_messages(self):
        for history_length in (0, 29, 30, 31, 40, 100):
            with self.subTest(history_length=history_length):
                conversation_id, history = await self._seed_conversation(history_length)
                response = await self._start_chat(conversation_id, use_tool=True)
                events = [self._event(chunk) async for chunk in response.body_iterator]
                self.assertNotIn("error", [event["type"] for event in events])

                messages = await self._read_messages(conversation_id)
                self.assertEqual(len(messages), history_length + 4)
                self.assertEqual(
                    [(message["role"], message["content"]) for message in messages[:history_length]],
                    [(message["role"], message["content"]) for message in history],
                )
                new_turn = messages[history_length:]
                self.assertEqual([message["role"] for message in new_turn], [
                    "user", "assistant", "tool", "assistant",
                ])
                self.assertEqual(new_turn[0]["content"], "Check inventory")
                self.assertEqual(new_turn[1]["tool_calls"][0]["call_id"], "call-inventory")
                self.assertEqual(new_turn[2]["tool_call_id"], "call-inventory")
                self.assertEqual(new_turn[2]["name"], "device_inventory_lookup")
                self.assertEqual(new_turn[3]["content"], "Inventory checked.")
                final = next(event for event in events if event["type"] == "final_answer")
                self.assertEqual(len(final["data"]["new_messages"]), 3)
                self.assertEqual(final["data"]["updated_messages"][-1]["content"], "Inventory checked.")

    async def test_final_answer_is_committed_before_delivery_and_client_disconnect(self):
        conversation_id, _ = await self._seed_conversation(40)
        response = await self._start_chat(conversation_id)
        self.assertEqual(self.active_sessions, set())
        try:
            async for chunk in response.body_iterator:
                event = self._event(chunk)
                if event["type"] == "final_answer":
                    self.assertEqual(len(self.active_sessions), 1)
                    messages = await self._read_messages(conversation_id)
                    self.assertEqual(len(messages), 42)
                    self.assertEqual(messages[-1]["content"], "Inventory checked.")
                    break
            else:
                self.fail("No final answer was delivered")
        finally:
            # Stop without asking the stream for its next event.
            await response.body_iterator.aclose()
        self.assertEqual(self.active_sessions, set())
        self.assertEqual(self.opened_sessions, 2)
        self.assertEqual(len(await self._read_messages(conversation_id)), 42)

    async def test_approval_request_and_assistant_call_are_committed_before_delivery(self):
        self.executor.needs_approval = True
        conversation_id, _ = await self._seed_conversation(40)
        response = await self._start_chat(conversation_id, use_tool=True)
        try:
            async for chunk in response.body_iterator:
                event = self._event(chunk)
                if event["type"] == "approval_required":
                    messages = await self._read_messages(conversation_id)
                    self.assertEqual(len(messages), 42)
                    self.assertEqual(messages[-1]["tool_calls"][0]["call_id"], "call-inventory")
                    async with self.session_factory() as session:
                        pending = await ApprovalRepository(session).list_pending(conversation_id)
                    self.assertEqual(pending[0]["call_id"], "call-inventory")
                    break
            else:
                self.fail("No approval request was delivered")
        finally:
            await response.body_iterator.aclose()
        self.assertEqual(self.active_sessions, set())

    async def test_failed_commit_emits_error_instead_of_final_answer(self):
        conversation_id, _ = await self._seed_conversation(40)
        response = await self._start_chat(conversation_id, use_tool=True)
        self.fail_stream_commits = True
        with self.assertLogs(chat_controller.logger, level="ERROR"):
            events = [self._event(chunk) async for chunk in response.body_iterator]
        self.assertNotIn("final_answer", [event["type"] for event in events])
        self.assertEqual(events[-1]["data"]["code"], "CHAT_STREAM_ERROR")
        self.assertEqual(len(await self._read_messages(conversation_id)), 41)
        self.assertEqual(self.active_sessions, set())

    async def test_failed_approval_message_commit_does_not_deliver_approval_required(self):
        self.executor.needs_approval = True
        conversation_id, _ = await self._seed_conversation(40)
        response = await self._start_chat(conversation_id, use_tool=True)
        original_save = chat_controller._save_new_messages

        async def fail_save(repo, conversation_id, messages):
            # The approval row was already committed by the engine; fail the
            # message batch to ensure the client is not told it can reload it.
            with patch.object(repo.session, "commit", side_effect=RuntimeError("DB unavailable")):
                await original_save(repo, conversation_id, messages)

        with (
            patch.object(chat_controller, "_save_new_messages", side_effect=fail_save),
            self.assertLogs(chat_controller.logger, level="ERROR"),
        ):
            events = [self._event(chunk) async for chunk in response.body_iterator]
        self.assertNotIn("approval_required", [event["type"] for event in events])
        self.assertEqual(events[-1]["data"]["code"], "CHAT_STREAM_ERROR")
        self.assertEqual(len(await self._read_messages(conversation_id)), 41)

    async def test_approval_outcome_is_saved_even_if_response_is_never_consumed(self):
        for approved in (False, True):
            with self.subTest(approved=approved):
                conversation_id, _ = await self._seed_conversation()
                async with self.session_factory() as session:
                    await ApprovalRepository(session).create_pending(
                        conversation_id=conversation_id,
                        call_id="call-restart",
                        tool_name="node_health_remediate",
                        arguments={"host": "test-node", "dry_run": False},
                        approval_metadata={
                            "host": "test-node",
                            "action": "restart_service",
                            "command": "docker restart test-service",
                        },
                        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
                    )
                with patch.object(
                    chat_controller.NodeHealthRemediateSkill,
                    "execute_approved_command",
                    return_value={"executed": True, "command_succeeded": True, "stderr": ""},
                ) as execute:
                    response = await chat_controller.approve_action(
                        chat_controller.ApprovalDecisionRequest(
                            conversation_id=conversation_id,
                            call_id="call-restart",
                            approved=approved,
                        )
                    )
                await response.body_iterator.aclose()
                messages = await self._read_messages(conversation_id)
                self.assertEqual([message["role"] for message in messages], ["tool", "assistant"])
                self.assertEqual(messages[0]["tool_call_id"], "call-restart")
                self.assertEqual(json.loads(messages[0]["content"])["executed"], approved)
                self.assertIn("Đã được duyệt" if approved else "Đã hủy", messages[-1]["content"])
                self.assertEqual(execute.call_count, int(approved))
                self.assertEqual(self.active_sessions, set())

    async def test_message_batch_rolls_back_as_a_unit(self):
        conversation_id, _ = await self._seed_conversation()
        async with self.session_factory() as session:
            with self.assertRaises(IntegrityError):
                await ConversationRepository(session).add_messages(conversation_id, [
                    {"role": "assistant", "tool_calls": [{"call_id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "result"},
                    {"role": None, "content": "Invalid final message"},
                ])
            await session.rollback()
        self.assertEqual(await self._read_messages(conversation_id), [])

    async def test_resumed_engine_does_not_reemit_loaded_history_as_new_messages(self):
        _, history = await self._seed_conversation(40)
        events = [event async for event in self.agent.run(
            session_id="resume-test",
            user_message="Do not append this user message",
            provider=ScriptedProvider(),
            history=history,
            resume=True,
        )]
        final = next(event for event in events if event.type == AgentEventType.FINAL_ANSWER)
        self.assertEqual(len(final.data["new_messages"]), 1)
        self.assertEqual(final.data["new_messages"][0].role, "assistant")
        self.assertEqual(final.data["new_messages"][0].content, "Inventory checked.")


if __name__ == "__main__":
    unittest.main()
