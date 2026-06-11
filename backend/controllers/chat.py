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

                    if updated and updated[0]["role"] == "user":
                        messages_to_save = updated[1:]
                    else:
                        logger.warning(
                            "updated_messages[0] is not user message, saving all"
                        )
                        messages_to_save = updated

                    for msg in messages_to_save:
                        await repo.add_message(
                            conversation.id,
                            role=msg["role"],
                            content=msg.get("content"),
                            tool_calls=msg.get("tool_calls"),
                            tool_call_id=msg.get("tool_call_id"),
                            name=msg.get("name"),
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
