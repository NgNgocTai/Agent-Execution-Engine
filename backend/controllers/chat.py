import logging
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.event_emitter import EventEmitter
from core.schemas import AgentEventType
from repositories.approval_repo import ApprovalRepository
from repositories.conversation_repo import ConversationRepository
from repositories.engine import async_session
from services.agent_service import AgentEngine
from services.providers.anthropic_provider import AnthropicProvider
from services.providers.base import LLMProvider
from services.providers.mock_provider import MockProvider
from services.providers.openai_provider import OpenAIProvider
from services.providers.deepseek_provider import DeepSeekProvider
from services.skills.base import SkillResult, SkillStatus
from services.skills.node_health_remediate import NodeHealthRemediateSkill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

MAX_USER_MESSAGE_CHARS = 5000


class ChatRequest(BaseModel):
    message: str
    provider: str = "mock"
    conversation_id: str | None = None

class ApprovalDecisionRequest(BaseModel):
    conversation_id: str
    call_id: str
    approved: bool

class RenameRequest(BaseModel):
    title: str

@router.put("/conversations/{conversation_id}/title")
async def rename_conversation(conversation_id: str, req: RenameRequest):
    async with async_session() as session:
        repo = ConversationRepository(session)
        success = await repo.rename(conversation_id, req.title)
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"success": True}


engine: AgentEngine | None = None


def set_engine(e: AgentEngine) -> None:
    global engine
    engine = e


def _get_provider(name: str) -> LLMProvider:
    providers = {
        "mock": MockProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "deepseek": DeepSeekProvider,
    }
    cls = providers.get(name, MockProvider)
    return cls()


AVAILABLE_PROVIDERS = ["openai", "anthropic", "deepseek"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _message_value(message, key: str, default=None):
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


async def _save_new_messages(
    repo: ConversationRepository,
    conversation_id: str,
    updated_messages: list,
    num_existing: int,
) -> None:
    messages_to_save = updated_messages[num_existing:]

    for msg in messages_to_save:
        tool_calls = _message_value(msg, "tool_calls")
        await repo.add_message(
            conversation_id,
            role=_message_value(msg, "role"),
            content=_message_value(msg, "content"),
            thinking_content=_message_value(msg, "thinking_content"),
            tool_calls=[tc.model_dump() for tc in tool_calls] if tool_calls else None,
            tool_call_id=_message_value(msg, "tool_call_id"),
            name=_message_value(msg, "tool_name"),
            is_error=_message_value(msg, "is_error", False),
        )


def _approval_summary(approved: bool, tool_result: dict, is_error: bool) -> str:
    action = tool_result.get("action", "remediation")
    host = tool_result.get("host", "unknown host")
    command = tool_result.get("command") or "không có command"

    if not approved:
        return (
            f"Đã hủy yêu cầu `{action}` trên `{host}`. "
            "Không có lệnh nào được thực thi."
        )

    if is_error:
        stderr = tool_result.get("stderr") or "không có stderr"
        return (
            f"Đã được duyệt nhưng lệnh `{command}` trên `{host}` không thành công. "
            f"Lỗi: {stderr}"
        )

    return (
        f"Đã được duyệt và đã thực thi lệnh `{command}` trên `{host}` thành công."
    )


@router.get("/config")
async def config():
    return {"providers": AVAILABLE_PROVIDERS}


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    async with async_session() as session:
        repo = ConversationRepository(session)
        conversation = await repo.get(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = await repo.get_messages(conversation_id)
        approval_repo = ApprovalRepository(session)
        pending_approvals = await approval_repo.list_pending(conversation_id)
        return {
            "conversation_id": conversation_id,
            "provider": conversation.provider,
            "messages": messages,
            "pending_approvals": pending_approvals,
        }


@router.get("/conversations")
async def list_conversations():
    async with async_session() as session:
        repo = ConversationRepository(session)
        conversations = await repo.list_all()
        return {"conversations": conversations}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    async with async_session() as session:
        repo = ConversationRepository(session)
        success = await repo.delete(conversation_id)
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"success": True}


@router.post("/chat/approve")
async def approve_action(req: ApprovalDecisionRequest):
    if engine is None:
        raise HTTPException(status_code=503, detail="AgentEngine not initialized")

    async with async_session() as session:
        repo = ConversationRepository(session)
        approval_repo = ApprovalRepository(session)

        conversation = await repo.get(req.conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        approval = await approval_repo.get_pending(req.conversation_id, req.call_id)
        if not approval:
            raise HTTPException(status_code=404, detail="Pending approval not found")

        if approval.expires_at <= _utcnow():
            await approval_repo.resolve(
                approval,
                status="expired",
                error="Approval expired before the user made a decision.",
            )
            raise HTTPException(status_code=409, detail="Approval expired")

        metadata = json.loads(approval.approval_metadata)

        if req.approved:
            command = metadata.get("command")
            host = metadata.get("host")
            if not command or not host:
                raise HTTPException(status_code=400, detail="Approval is missing command or host")

            tool = NodeHealthRemediateSkill()
            command_result = tool.execute_approved_command(host=host, command=command)
            tool_result = {
                **metadata,
                **command_result,
                "approval_status": "approved",
            }
            is_error = not bool(command_result.get("command_succeeded"))
            await approval_repo.resolve(
                approval,
                status="approved",
                result=tool_result,
                error=command_result.get("stderr") if is_error else None,
            )
        else:
            tool_result = {
                **metadata,
                "approval_status": "rejected",
                "executed": False,
                "command_succeeded": False,
                "exit_code": None,
                "stdout": None,
                "stderr": "User rejected the requested remediation.",
            }
            is_error = True
            await approval_repo.resolve(
                approval,
                status="rejected",
                result=tool_result,
                error="User rejected the requested remediation.",
            )

        await repo.add_message(
            conversation.id,
            role="tool",
            content=json.dumps(tool_result, ensure_ascii=False),
            tool_call_id=approval.call_id,
            name=approval.tool_name,
            is_error=is_error,
        )

        async def generate():
            try:
                result_status = (
                    SkillStatus.SYSTEM_ERROR
                    if req.approved and is_error
                    else SkillStatus.BUSINESS_ERROR
                    if not req.approved
                    else SkillStatus.SUCCESS
                )
                yield EventEmitter(conversation.id).tool_call_result(
                    call_id=approval.call_id,
                    tool_name=approval.tool_name,
                    result=SkillResult(
                        status=result_status,
                        data=tool_result,
                        error=tool_result.get("stderr") if is_error else None,
                    ),
                    iteration=0,
                ).to_sse()

                summary = _approval_summary(req.approved, tool_result, is_error)
                await repo.add_message(
                    conversation.id,
                    role="assistant",
                    content=summary,
                )
                yield EventEmitter(conversation.id).text_delta(summary).to_sse()
                yield EventEmitter(conversation.id).final_answer(
                    total_tool_calls=0,
                    total_duration_ms=0,
                    updated_messages=[],
                ).to_sse()
                yield f'data: {{"type":"conversation_id","data":{{"conversation_id":"{conversation.id}"}}}}\n\n'
            except Exception as exc:
                logger.exception("Approval stream failed")
                yield EventEmitter(conversation.id).error(
                    message=f"Lỗi xử lý approval stream: {exc}",
                    code="APPROVAL_STREAM_ERROR",
                ).to_sse()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


@router.post("/chat")
async def chat(req: ChatRequest):
    if engine is None:
        raise HTTPException(status_code=503, detail="AgentEngine not initialized")

    if len(req.message) > MAX_USER_MESSAGE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Nội dung quá dài, vui lòng rút gọn hoặc chia thành nhiều câu hỏi."
            ),
        )

    async with async_session() as session:
        repo = ConversationRepository(session)
        approval_repo = ApprovalRepository(session)

        if req.conversation_id:
            conversation = await repo.get(req.conversation_id)
            if not conversation:
                raise HTTPException(status_code=404, detail="Conversation not found")
        else:
            conversation = await repo.create(provider=req.provider)

        history = await repo.get_messages(conversation.id)

        await repo.add_message(
            conversation.id, role="user", content=req.message
        )

        provider = _get_provider(req.provider)

        async def generate():
            try:
                async for event in engine.run(
                    session_id=conversation.id,
                    user_message=req.message,
                    provider=provider,
                    history=history,
                    approval_repo=approval_repo,
                ):
                    yield event.to_sse()

                    if event.type in (
                        AgentEventType.FINAL_ANSWER,
                        AgentEventType.APPROVAL_REQUIRED,
                    ):
                        updated = event.data.get("updated_messages", [])

                        # `updated` chứa: [toàn bộ history] + [user_message] + [các message mới tạo ra (assistant, tool)]
                        # Ta chỉ cần lưu các message mới tạo ra
                        num_existing = len(history) + 1
                        await _save_new_messages(repo, conversation.id, updated, num_existing)
                        yield f'data: {{"type":"conversation_id","data":{{"conversation_id":"{conversation.id}"}}}}\n\n'
            except Exception as exc:
                logger.exception("Chat stream failed")
                yield EventEmitter(conversation.id).error(
                    message=f"Lỗi xử lý chat stream: {exc}",
                    code="CHAT_STREAM_ERROR",
                ).to_sse()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
