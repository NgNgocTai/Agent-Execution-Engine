import asyncio
import logging
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from core.event_emitter import EventEmitter
from core.policy import approved_snapshot
from core.auth import require
from core.redaction import redact
from core.config import settings
from repositories.audit_repo import audit
from repositories.run_models import Run, RunCheckpoint
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
from services.providers.lifecycle import close_provider
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
    require("operator")
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
    from services.run_configuration import execution_settings
    settings = execution_settings()
    providers = {
        "mock": MockProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "deepseek": DeepSeekProvider,
    }
    cls = providers.get(name)
    if cls is None:
        raise HTTPException(422, "Unknown provider")
    provider = cls(delay_seconds=settings.fixture_delay_seconds) if name == "mock" and settings.data_mode == "fixture" else cls()
    provider.fallbacks = [providers[candidate.strip()]() for candidate in settings.llm_fallback_providers.split(",")
                          if candidate.strip() in providers and candidate.strip() not in {name, "mock"}]
    return provider


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
    new_messages: list,
) -> None:
    messages_to_save = []
    for msg in new_messages:
        tool_calls = _message_value(msg, "tool_calls")
        messages_to_save.append({
            "role": _message_value(msg, "role"),
            "content": _message_value(msg, "content"),
            "thinking_content": _message_value(msg, "thinking_content"),
            "tool_calls": [
                tc if isinstance(tc, dict) else tc.model_dump() for tc in tool_calls
            ] if tool_calls else None,
            "tool_call_id": _message_value(msg, "tool_call_id"),
            "name": _message_value(msg, "tool_name"),
            "is_error": _message_value(msg, "is_error", False),
        })
    await repo.add_messages(conversation_id, messages_to_save)


def _approval_summary(approved: bool, tool_result: dict, is_error: bool) -> str:
    action = tool_result.get("action", "remediation")
    host = tool_result.get("host", "unknown host")
    command = tool_result.get("command") or "không có command"

    if tool_result.get("approval_status") == "unknown":
        return (
            f"Chưa xác định kết quả lệnh `{command}` trên `{host}`. "
            "Lệnh có thể đã được gửi; cần đối soát trên node trước khi xử lý tiếp. "
            "Hệ thống không tự động chạy lại lệnh này."
        )

    if not approved:
        return (
            f"Đã hủy yêu cầu `{action}` trên `{host}`. "
            "Không có lệnh nào được thực thi."
        )

    if tool_result.get("execution_status") == "not_started":
        return (
            f"Đã được duyệt nhưng chưa thực thi lệnh `{command}` trên `{host}`. "
            f"Lỗi: {tool_result.get('stderr') or 'SSH chưa sẵn sàng'}"
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


def _unknown_approval_result(metadata: dict, details: dict | None = None) -> dict:
    details = details or {}
    return {
        **metadata,
        "approval_status": "unknown",
        "executed": None,
        "command_succeeded": False,
        "exit_code": None,
        "stdout": details.get("stdout"),
        "stderr": details.get("stderr") or "Command outcome is unknown; reconcile the node before any further action.",
        "stdout_truncated": details.get("stdout_truncated", False),
        "stderr_truncated": details.get("stderr_truncated", False),
        "execution_status": "unknown",
        "error_code": details.get("error_code"),
    }


async def _save_approval_outcome(repo, approval_repo, approval, conversation_id, approved, tool_result):
    is_error = not approved or not bool(tool_result.get("command_succeeded"))
    summary = _approval_summary(approved, tool_result, is_error)
    changed = await approval_repo.resolve(
        approval,
        status=tool_result["approval_status"],
        result=tool_result,
        error=tool_result.get("stderr") if is_error else None,
        commit=False,
    )
    if not changed:
        raise HTTPException(status_code=409, detail="Approval state changed before completion")
    # Resolve and both messages commit together; a failed write leaves the
    # previously committed claim in place so a retry cannot repeat the command.
    await repo.add_messages(conversation_id, [
        {
            "role": "tool",
            "content": json.dumps(tool_result, ensure_ascii=False),
            "tool_call_id": approval.call_id,
            "name": approval.tool_name,
            "is_error": is_error,
        },
        {"role": "assistant", "content": summary},
    ])
    return summary, is_error


def _approval_response(conversation_id, call_id, tool_name, approved, tool_result):
    is_error = not approved or not bool(tool_result.get("command_succeeded"))
    summary = _approval_summary(approved, tool_result, is_error)
    emitter = EventEmitter(conversation_id)

    async def generate():
        result_status = (
            SkillStatus.SYSTEM_ERROR if approved and is_error
            else SkillStatus.BUSINESS_ERROR if not approved
            else SkillStatus.SUCCESS
        )
        yield emitter.tool_call_result(
            call_id=call_id,
            tool_name=tool_name,
            result=SkillResult(
                status=result_status,
                data=tool_result,
                error=tool_result.get("stderr") if is_error else None,
            ),
            iteration=0,
        ).to_sse()
        yield emitter.text_delta(summary).to_sse()
        yield emitter.final_answer(
            total_tool_calls=0, total_duration_ms=0, updated_messages=[],
        ).to_sse()
        yield f'data: {{"type":"conversation_id","data":{{"conversation_id":"{conversation_id}"}}}}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/config")
async def config():
    actor = require("viewer", "operator", "approver", "skill_reviewer")
    return {"providers": ["mock"] if settings.data_mode == "fixture" else AVAILABLE_PROVIDERS,
            "actor": {"subject": actor.subject, "roles": sorted(actor.roles)}}


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    require("viewer", "operator", "approver", "skill_reviewer")
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
            "messages": redact(messages),
            "pending_approvals": redact(pending_approvals),
        }


@router.get("/conversations")
async def list_conversations():
    require("viewer", "operator", "approver", "skill_reviewer")
    async with async_session() as session:
        repo = ConversationRepository(session)
        conversations = await repo.list_all()
        return {"conversations": redact(conversations)}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    require("operator")
    async with async_session() as session:
        repo = ConversationRepository(session)
        success = await repo.delete(conversation_id)
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"success": True}


@router.post("/chat/approve")
async def approve_action(req: ApprovalDecisionRequest):
    require("approver")
    if engine is None:
        raise HTTPException(status_code=503, detail="AgentEngine not initialized")

    async with async_session() as session:
        repo = ConversationRepository(session)
        approval_repo = ApprovalRepository(session)

        conversation = await repo.get(req.conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        approval = await approval_repo.get(req.conversation_id, req.call_id)
        if not approval:
            raise HTTPException(status_code=404, detail="Approval not found")

        if approval.status in {"approved", "rejected"}:
            if req.approved != (approval.status == "approved"):
                raise HTTPException(status_code=409, detail="Approval already has a different decision")
            if not approval.result:
                raise HTTPException(status_code=409, detail="Approval result requires reconciliation")
            return _approval_response(
                req.conversation_id, approval.call_id, approval.tool_name,
                req.approved, json.loads(approval.result),
            )
        if approval.status != "pending":
            raise HTTPException(
                status_code=409,
                detail="Approval is already claimed or requires reconciliation; do not rerun the command",
            )

        if approval.expires_at <= _utcnow():
            await approval_repo.resolve(
                approval,
                status="expired",
                error="Approval expired before the user made a decision.",
            )
            raise HTTPException(status_code=409, detail="Approval expired")

        metadata = json.loads(approval.approval_metadata)
        if req.approved:
            active_run = await session.scalar(select(Run).where(Run.conversation_id == req.conversation_id, Run.active_key.is_not(None)))
            if active_run:
                from services.run_configuration import load_configuration
                from services.workflow.state import CheckpointIncompatible
                try:
                    load_configuration(active_run.execution_config, active_run.provider)
                except CheckpointIncompatible:
                    raise HTTPException(409, "Run configuration is missing or invalid; cancel and start a new run") from None
                checkpoint = await session.get(RunCheckpoint, active_run.id)
                state = json.loads(checkpoint.state) if checkpoint else {}
                pending = state.get("pending_calls", [])
                if active_run.status != "waiting_approval" or not pending or pending[0]["call_id"] != req.call_id:
                    raise HTTPException(409, "Approval does not match the active run checkpoint")
            command = metadata.get("command")
            host = metadata.get("host")
            if not command or not host:
                raise HTTPException(status_code=400, detail="Approval is missing command or host")
            if approval.tool_name != "node_health_remediate":
                raise HTTPException(status_code=400, detail="Unsupported remediation tool")
            if not approved_snapshot(metadata):
                await audit(session, "approval.denied", req.call_id, {"reason": "policy"})
                await session.commit()
                raise HTTPException(status_code=403, detail="Approval action is outside the current policy")

        if not await approval_repo.claim_pending(approval, approved=req.approved):
            raise HTTPException(status_code=409, detail="Approval expired, changed, or was claimed concurrently")

        await audit(session, "approval.claimed", req.call_id, {"approved": req.approved})
        await session.commit()

        try:
            if req.approved:
                try:
                    command_result = await asyncio.wait_for(asyncio.to_thread(
                        NodeHealthRemediateSkill().execute_approved_command,
                        host=host, command=command,
                    ), timeout=settings.approval_action_timeout_seconds)
                except Exception:
                    command_result = {}
                    logger.exception("Approved command outcome could not be confirmed")
                if (
                    not command_result.get("command_succeeded")
                    and command_result.get("exit_code") is None
                    and command_result.get("execution_status") != "not_started"
                ):
                    tool_result = _unknown_approval_result(metadata, command_result)
                else:
                    tool_result = {**metadata, **command_result, "approval_status": "approved"}
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
            await audit(session, "approval.resolved", req.call_id, {"status": tool_result["approval_status"]})
            await _save_approval_outcome(
                repo, approval_repo, approval, req.conversation_id, req.approved, tool_result,
            )
        except asyncio.CancelledError:
            # Cancelling the await cannot stop an SSH command already sent by
            # the thread. Keep its durable claim and record the ambiguity.
            try:
                await session.rollback()
                current = await approval_repo.get(req.conversation_id, req.call_id)
                if current is not None and current.status == "executing":
                    await _save_approval_outcome(
                        repo, approval_repo, current, req.conversation_id, True,
                        _unknown_approval_result(metadata),
                    )
            except Exception:
                logger.exception("Approval claim retained; cancellation outcome requires reconciliation")
            raise
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Approval claim retained after persistence failure")
            raise HTTPException(
                status_code=503,
                detail="Unable to save approval outcome; reconcile before rerunning any command",
            ) from exc

        return _approval_response(
            req.conversation_id, approval.call_id, approval.tool_name, req.approved, tool_result,
        )


@router.post("/chat")
async def chat(req: ChatRequest):
    require("operator")
    if settings.deployment_mode == "production":
        raise HTTPException(410, "Use POST /api/runs for durable execution")
    if settings.data_mode == "fixture" and req.provider != "mock":
        raise HTTPException(422, "Fixture mode only supports the mock provider")
    agent_engine = engine
    if agent_engine is None:
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
        if req.conversation_id:
            conversation = await repo.get(req.conversation_id)
            if not conversation:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if await session.scalar(select(Run.id).where(Run.conversation_id == req.conversation_id, Run.active_key.is_not(None)).limit(1)):
                raise HTTPException(409, "Conversation has an active run; use its run endpoints")
        else:
            conversation = await repo.create(provider=req.provider)

        history = await repo.get_messages(conversation.id)

        await repo.add_message(
            conversation.id, role="user", content=req.message
        )

        provider = _get_provider(req.provider)
        conversation_id = conversation.id

    async def generate():
        try:
            # The stream owns its session for the full iteration lifetime.
            async with async_session() as stream_session:
                stream_repo = ConversationRepository(stream_session)
                approval_repo = ApprovalRepository(stream_session)
                async for event in agent_engine.run(
                    session_id=conversation_id,
                    user_message=req.message,
                    provider=provider,
                    history=history,
                    approval_repo=approval_repo,
                ):
                    persist_messages = event.type in (
                        AgentEventType.FINAL_ANSWER,
                        AgentEventType.APPROVAL_REQUIRED,
                    ) or (event.type == AgentEventType.ERROR and "new_messages" in event.data)
                    if persist_messages:
                        await _save_new_messages(
                            stream_repo, conversation_id, event.data["new_messages"]
                        )
                    # Never advertise a completed turn or pending approval
                    # before the messages needed to reload it are committed.
                    yield event.to_sse()
                    if persist_messages:
                        yield f'data: {{"type":"conversation_id","data":{{"conversation_id":"{conversation_id}"}}}}\n\n'
        except Exception as exc:
            logger.exception("Chat stream failed")
            yield EventEmitter(conversation_id).error(
                message="Chat stream failed. Please retry or reload the conversation.",
                code="CHAT_STREAM_ERROR",
            ).to_sse()
        finally:
            await close_provider(provider)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
