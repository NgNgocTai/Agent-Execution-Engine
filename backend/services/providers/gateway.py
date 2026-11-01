"""Bounded provider calls with a stable, public error contract."""
import asyncio
from contextlib import aclosing
import random
import json

from core.config import settings
from core.schemas import NormalizedResponse
from core.schemas import NormalizedMessage
from core.redaction import redact
from services.context_manager import prepare_context, ContextBudgetExceeded


async def complete(provider, messages, tools):
    # A partial stream is never retried or combined with another provider.
    try:
        messages = [NormalizedMessage.model_validate(redact(message)) for message in messages]
        messages = prepare_context(messages, tools, settings.llm_context_tokens, settings.llm_reserved_output_tokens, settings.llm_context_margin, summarize=settings.context_summary_enabled)
        async with asyncio.timeout(settings.llm_timeout_seconds):
            candidates = [provider, *getattr(provider, "fallbacks", [])]
            for candidate_index, candidate in enumerate(candidates):
                for attempt in range(settings.llm_max_attempts):
                    emitted = False
                    error = None
                    output_bytes = 0
                    tool_count = 0
                    transient = False
                    try:
                        async with aclosing(candidate.chat_completion(messages, tools)) as stream:
                            async for response in stream:
                                if response.provider_error:
                                    kind = response.provider_error.get("error_type", "")
                                    transient = kind in {"RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"}
                                    error = "PROVIDER_UNAVAILABLE" if transient else "PROVIDER_ERROR"
                                    break
                                output_bytes += len((response.text_content or "").encode()) + len((response.thinking_content or "").encode())
                                output_bytes += len(json.dumps([call.model_dump() for call in response.tool_calls], ensure_ascii=False).encode())
                                tool_count += len(response.tool_calls)
                                if tool_count > settings.llm_tool_batch_limit:
                                    yield NormalizedResponse(provider_error={"code": "TOOL_BATCH_LIMIT"})
                                    return
                                if output_bytes > settings.llm_output_max_bytes:
                                    yield NormalizedResponse(provider_error={"code": "PROVIDER_OUTPUT_LIMIT"})
                                    return
                                emitted |= bool(response.text_content or response.thinking_content or response.tool_calls)
                                yield response
                    except Exception as exc:
                        status = getattr(exc, "status_code", None)
                        transient = isinstance(exc, (ConnectionError, TimeoutError)) or status in {408, 429} or (isinstance(status, int) and status >= 500)
                        error = "PROVIDER_UNAVAILABLE" if transient else "PROVIDER_ERROR"
                    if not error and emitted:
                        return
                    if emitted:
                        yield NormalizedResponse(provider_error={"code": error or "PROVIDER_EMPTY"})
                        return
                    if transient and attempt + 1 < settings.llm_max_attempts:
                        await asyncio.sleep(0.2 * 2 ** attempt + random.uniform(0, 0.1))
                        continue
                    break
                if candidate_index == len(candidates) - 1:
                    yield NormalizedResponse(provider_error={"code": error or "PROVIDER_EMPTY"})
    except ContextBudgetExceeded:
        yield NormalizedResponse(provider_error={"code": "CONTEXT_BUDGET_EXCEEDED"})
    except TimeoutError:
        yield NormalizedResponse(provider_error={"code": "PROVIDER_TIMEOUT"})
    except Exception:
        yield NormalizedResponse(provider_error={"code": "PROVIDER_ERROR"})
