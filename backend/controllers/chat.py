import logging
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.agent_service import AgentEngine
from services.providers.mock_provider import MockProvider
from services.providers.openai_provider import OpenAIProvider
from services.providers.anthropic_provider import AnthropicProvider
from services.providers.base import LLMProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    provider: str = "mock"


engine: AgentEngine | None = None


def set_engine(e: AgentEngine) -> None:
    global engine
    engine = e


async def event_stream(gen: AsyncGenerator) -> AsyncGenerator[str, None]:
    async for event in gen:
        yield event.to_sse()


def _get_provider(name: str) -> LLMProvider:
    providers = {
        "mock": MockProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
    }
    cls = providers.get(name, MockProvider)
    return cls()


@router.post("/chat")
async def chat(req: ChatRequest):
    if engine is None:
        return {"error": "AgentEngine not initialized"}

    provider = _get_provider(req.provider)

    async def generate():
        async for event in engine.run(
            session_id="test-session",
            user_message=req.message,
            provider=provider,
            history=req.history,
        ):
            yield event.to_sse()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
