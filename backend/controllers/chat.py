import logging
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from repositories.conversation_repo import ConversationRepository
from repositories.engine import async_session
from services.agent_service import AgentEngine
from services.providers.anthropic_provider import AnthropicProvider
from services.providers.base import LLMProvider
from services.providers.mock_provider import MockProvider
from services.providers.openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    provider: str = "mock"
    conversation_id: str | None = None

class RenameRequest(BaseModel):
    title: str

@router.put("/conversations/{conversation_id}/title")
async def rename_conversation(conversation_id: str, req: RenameRequest):
    async with async_session() as session:
        repo = ConversationRepository(session)
        success = await repo.rename(conversation_id, req.title)
        if not success:
            return {"error": "Conversation not found"}
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
    }
    cls = providers.get(name, MockProvider)
    return cls()


AVAILABLE_PROVIDERS = ["openai", "anthropic"]


@router.get("/config")
async def config():
    return {"providers": AVAILABLE_PROVIDERS}


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    async with async_session() as session:
        repo = ConversationRepository(session)
        conversation = await repo.get(conversation_id)
        if not conversation:
            return {"error": "Conversation not found"}
        messages = await repo.get_messages(conversation_id)
        return {
            "conversation_id": conversation_id,
            "provider": conversation.provider,
            "messages": messages,
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
            return {"error": "Conversation not found"}
        return {"success": True}


@router.post("/chat")
async def chat(req: ChatRequest):
    if engine is None:
        return {"error": "AgentEngine not initialized"}

    async with async_session() as session:
        repo = ConversationRepository(session)

        if req.conversation_id:
            conversation = await repo.get(req.conversation_id)
            if not conversation:
                return {"error": "Conversation not found"}
        else:
            conversation = await repo.create(provider=req.provider)

        history = await repo.get_messages(conversation.id)

        await repo.add_message(
            conversation.id, role="user", content=req.message
        )

        provider = _get_provider(req.provider)

        async def generate():
            async for event in engine.run(
                session_id=conversation.id,
                user_message=req.message,
                provider=provider,
                history=history,
            ):
                yield event.to_sse()

                if event.type == "final_answer":
                    updated = event.data.get("updated_messages", [])

                    # `updated` chứa: [toàn bộ history] + [user_message] + [các message mới tạo ra (assistant, tool)]
                    # Ta chỉ cần lưu các message mới tạo ra
                    num_existing = len(history) + 1
                    messages_to_save = updated[num_existing:]

                    for msg in messages_to_save:
                        await repo.add_message(
                            conversation.id,
                            role=msg.role,
                            content=msg.content,
                            tool_calls=[tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
                            tool_call_id=msg.tool_call_id,
                            name=msg.tool_name,
                            is_error=msg.is_error,
                        )
                    yield f'data: {{"type":"conversation_id","data":{{"conversation_id":"{conversation.id}"}}}}\n\n'

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
