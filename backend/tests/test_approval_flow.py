import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from core.schemas import AgentEventType, NormalizedResponse, NormalizedToolCall
from controllers import chat as chat_controller
from services.agent_service import AgentEngine
from services.skills.base import SkillResult, SkillStatus
from services.skills.node_health_remediate import NodeHealthRemediateSkill


class FakeRemediateSkill(NodeHealthRemediateSkill):
    def __init__(self, metrics):
        self.metrics = metrics
        self.executed_commands = []

    def _get_metrics(self, host: str) -> dict:
        return self.metrics

    def _ssh_exec(self, host: str, command: str):
        self.executed_commands.append((host, command))
        return 0, "ok", ""


class FakeApprovalRepo:
    def __init__(self):
        self.created = []

    async def create_pending(self, **kwargs):
        self.created.append(kwargs)
        return kwargs


class FakeSessionContext:
    async def __aenter__(self):
        return SimpleNamespace(add=lambda record: None, flush=AsyncMock(), commit=AsyncMock(), scalar=AsyncMock(return_value=None))

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConversationRepoForApproval:
    def __init__(self, session):
        self.messages = []
        self.history_messages = []
        self.conversation = SimpleNamespace(id="conv-1", provider="openai")

    async def get(self, conversation_id):
        return self.conversation if conversation_id == self.conversation.id else None

    async def get_messages(self, conversation_id):
        return self.history_messages

    async def add_message(self, conversation_id, **kwargs):
        self.messages.append({"conversation_id": conversation_id, **kwargs})

    async def add_messages(self, conversation_id, messages):
        for message in messages:
            await self.add_message(conversation_id, **message)


class FakeApprovalRepoForController:
    def __init__(self, session):
        self.approval = SimpleNamespace(
            status="pending",
            result=None,
            call_id="call_restart",
            tool_name="node_health_remediate",
            approval_metadata=json.dumps(
                {
                    "host": "NOC-APP-01",
                    "service": "noc-api",
                    "action": "restart_service",
                    "command": "docker restart noc-api",
                },
                ensure_ascii=False,
            ),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        )
        self.resolved = []
        self.pending = [
            {
                "call_id": "call_restart",
                "tool_name": "node_health_remediate",
                "arguments": {"host": "NOC-APP-01", "dry_run": False},
                "approval": {"command": "docker restart noc-api"},
            }
        ]

    async def get_pending(self, conversation_id, call_id):
        if conversation_id == "conv-1" and call_id == self.approval.call_id:
            return self.approval
        return None

    async def get(self, conversation_id, call_id):
        return await self.get_pending(conversation_id, call_id)

    async def claim_pending(self, approval, *, approved):
        if approval.status != "pending":
            return False
        approval.status = "executing" if approved else "rejecting"
        return True

    async def list_pending(self, conversation_id):
        return self.pending if conversation_id == "conv-1" else []

    async def resolve(self, approval, status, result=None, error=None, *, commit=True):
        self.resolved.append(
            {
                "approval": approval,
                "status": status,
                "result": result,
                "error": error,
            }
        )
        approval.status = status
        return True


class FakeNodeHealthRemediateSkillForApproval:
    calls = []

    def execute_approved_command(self, host: str, command: str):
        self.calls.append((host, command))
        return {
            "executed": True,
            "command_succeeded": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }


class ApprovalFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Explicitly scoped test deployment; production defaults deny writes.
        from core.config import settings
        self.enterContext(patch.object(settings, "ssh_allowed_hosts", "test-node,NOC-APP-01"))
        self.enterContext(patch.object(settings, "ssh_allowed_services", "test-service,noc-api"))

    async def _collect_sse(self, response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    async def test_dangerous_remediate_requires_approval_without_executing(self):
        skill = FakeRemediateSkill({"mem_pct": 10, "cpu_pct": 95})

        result = await skill.execute(
            host="NOC-APP-01",
            cpu_threshold=80,
            service="noc-api",
            dry_run=False,
        )

        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertTrue(result.needs_approval)
        self.assertEqual(result.approval_metadata["action"], "restart_service")
        self.assertEqual(result.approval_metadata["command"], "docker restart noc-api")
        self.assertEqual(skill.executed_commands, [])

    async def test_read_logs_can_execute_without_approval(self):
        skill = FakeRemediateSkill({"mem_pct": 95, "cpu_pct": 10})

        result = await skill.execute(
            host="NOC-APP-01",
            ram_threshold=80,
            service="noc-api",
            dry_run=False,
        )

        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertFalse(result.needs_approval)
        self.assertTrue(result.data["executed"])
        self.assertEqual(skill.executed_commands[0][1], "docker logs --timestamps --tail 200 noc-api")

    async def test_agent_emits_approval_required_and_stops(self):
        class FakeProvider:
            model_name = "fake"

            async def chat_completion(self, messages, tools, stream=True):
                yield NormalizedResponse(
                    tool_calls=[
                        NormalizedToolCall(
                            call_id="call_restart",
                            tool_name="node_health_remediate",
                            arguments={"host": "NOC-APP-01", "dry_run": False},
                        )
                    ],
                    stop_reason="tool_calls",
                )

        class FakeToolExecutor:
            async def execute(self, tool_name: str, arguments: dict):
                return SkillResult(
                    status=SkillStatus.SUCCESS,
                    data={"approval_status": "pending"},
                    needs_approval=True,
                    approval_metadata={
                        "host": "NOC-APP-01",
                        "action": "restart_docker",
                        "command": "sudo -n systemctl restart docker",
                    },
                )

        class FakeSkillRegistry:
            def get_tool_schemas(self):
                return []

        approval_repo = FakeApprovalRepo()
        engine = AgentEngine(
            tool_executor=FakeToolExecutor(),
            skill_registry=FakeSkillRegistry(),
            tracer=None,
        )

        events = [
            event
            async for event in engine.run(
                session_id="session-approval",
                user_message="restart node",
                provider=FakeProvider(),
                history=[],
                approval_repo=approval_repo,
            )
        ]

        event_types = [event.type for event in events]
        self.assertIn(AgentEventType.APPROVAL_REQUIRED, event_types)
        self.assertNotIn(AgentEventType.TOOL_CALL_RESULT, event_types)
        self.assertNotIn(AgentEventType.FINAL_ANSWER, event_types)
        self.assertEqual(approval_repo.created[0]["call_id"], "call_restart")
        self.assertEqual(
            approval_repo.created[0]["approval_metadata"]["command"],
            "sudo -n systemctl restart docker",
        )

    async def test_reject_pending_approval_does_not_execute_and_returns_short_summary(self):
        conversation_repo = FakeConversationRepoForApproval(None)
        approval_repo = FakeApprovalRepoForController(None)
        previous_engine = chat_controller.engine
        chat_controller.engine = object()

        try:
            with (
                patch.object(chat_controller, "async_session", return_value=FakeSessionContext()),
                patch.object(chat_controller, "ConversationRepository", return_value=conversation_repo),
                patch.object(chat_controller, "ApprovalRepository", return_value=approval_repo),
                patch.object(
                    chat_controller,
                    "NodeHealthRemediateSkill",
                    FakeNodeHealthRemediateSkillForApproval,
                ),
            ):
                FakeNodeHealthRemediateSkillForApproval.calls = []
                response = await chat_controller.approve_action(
                    chat_controller.ApprovalDecisionRequest(
                        conversation_id="conv-1",
                        call_id="call_restart",
                        approved=False,
                    )
                )
                sse = await self._collect_sse(response)
        finally:
            chat_controller.engine = previous_engine

        self.assertEqual(FakeNodeHealthRemediateSkillForApproval.calls, [])
        self.assertEqual(approval_repo.resolved[0]["status"], "rejected")
        self.assertEqual(approval_repo.resolved[0]["result"]["executed"], False)
        self.assertIn("Không có lệnh nào được thực thi", sse)
        self.assertIn('"type":"final_answer"', sse)
        self.assertEqual(conversation_repo.messages[-1]["role"], "assistant")
        self.assertIn("Đã hủy yêu cầu", conversation_repo.messages[-1]["content"])

    async def test_approve_pending_approval_executes_saved_command_once(self):
        conversation_repo = FakeConversationRepoForApproval(None)
        approval_repo = FakeApprovalRepoForController(None)
        previous_engine = chat_controller.engine
        chat_controller.engine = object()

        try:
            with (
                patch.object(chat_controller, "async_session", return_value=FakeSessionContext()),
                patch.object(chat_controller, "ConversationRepository", return_value=conversation_repo),
                patch.object(chat_controller, "ApprovalRepository", return_value=approval_repo),
                patch.object(
                    chat_controller,
                    "NodeHealthRemediateSkill",
                    FakeNodeHealthRemediateSkillForApproval,
                ),
            ):
                FakeNodeHealthRemediateSkillForApproval.calls = []
                response = await chat_controller.approve_action(
                    chat_controller.ApprovalDecisionRequest(
                        conversation_id="conv-1",
                        call_id="call_restart",
                        approved=True,
                    )
                )
                sse = await self._collect_sse(response)
        finally:
            chat_controller.engine = previous_engine

        self.assertEqual(
            FakeNodeHealthRemediateSkillForApproval.calls,
            [("NOC-APP-01", "docker restart noc-api")],
        )
        self.assertEqual(approval_repo.resolved[0]["status"], "approved")
        self.assertEqual(approval_repo.resolved[0]["result"]["command_succeeded"], True)
        self.assertIn("Đã được duyệt và đã thực thi", sse)
        self.assertIn('"type":"final_answer"', sse)

    async def test_loading_conversation_returns_pending_approvals_for_reload(self):
        conversation_repo = FakeConversationRepoForApproval(None)
        approval_repo = FakeApprovalRepoForController(None)

        with (
            patch.object(chat_controller, "async_session", return_value=FakeSessionContext()),
            patch.object(chat_controller, "ConversationRepository", return_value=conversation_repo),
            patch.object(chat_controller, "ApprovalRepository", return_value=approval_repo),
        ):
            payload = await chat_controller.get_conversation_messages("conv-1")

        self.assertEqual(payload["conversation_id"], "conv-1")
        self.assertEqual(payload["pending_approvals"][0]["call_id"], "call_restart")
        self.assertEqual(
            payload["pending_approvals"][0]["approval"]["command"],
            "docker restart noc-api",
        )


if __name__ == "__main__":
    unittest.main()
