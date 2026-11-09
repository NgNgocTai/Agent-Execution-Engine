"""Provider stream completion and tool arguments are untrusted contracts."""

import json
import math

from core.schemas import NormalizedResponse, NormalizedToolCall
from services.providers.thinking import ThinkTagParser, get_reasoning_delta


def normalize_stop(reason):
    return {
        "stop": "end_turn",
        "end_turn": "end_turn",
        "stop_sequence": "end_turn",
        "tool_calls": "tool_calls",
        "tool_use": "tool_calls",
        "length": "length",
        "max_tokens": "length",
        "content_filter": "content_filter",
        "refusal": "content_filter",
    }.get(reason, "unknown")


def completion_error(reason):
    return {"length": "PROVIDER_TRUNCATED", "content_filter": "PROVIDER_FILTERED"}.get(
        reason, "PROVIDER_INCOMPLETE"
    )


def strict_arguments(raw):
    def reject_constant(value):
        raise ValueError("Non-finite tool argument")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate tool argument")
            result[key] = value
        return result

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite tool argument")
        return number

    value = json.loads(
        raw,
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=unique_pairs,
    )
    if not isinstance(value, dict):
        raise ValueError("Tool arguments must be an object")
    return value


async def openai_chunks(stream):
    from services.run_configuration import execution_settings

    config = execution_settings()
    buffers, parser, finished = {}, ThinkTagParser(), False
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            yield NormalizedResponse(
                token_usage={
                    "input": chunk.usage.prompt_tokens,
                    "output": chunk.usage.completion_tokens,
                }
            )
        if not chunk.choices:
            continue
        if finished:
            yield NormalizedResponse(provider_error={"code": "PROVIDER_INCOMPLETE"})
            return
        choice = chunk.choices[0]
        delta = choice.delta
        if delta is not None:
            for part in getattr(delta, "tool_calls", None) or []:
                buf = buffers.setdefault(
                    part.index, {"id": "", "name": "", "arguments": ""}
                )
                if part.id:
                    buf["id"] = part.id
                if part.function:
                    buf["name"] += part.function.name or ""
                    buf["arguments"] += part.function.arguments or ""
            if (
                len(buffers) > config.llm_tool_batch_limit
                or sum(
                    len(buf["arguments"].encode())
                    + len(buf["name"].encode())
                    + len(buf["id"].encode())
                    for buf in buffers.values()
                )
                > config.llm_output_max_bytes
            ):
                yield NormalizedResponse(
                    provider_error={"code": "PROVIDER_OUTPUT_LIMIT"}
                )
                return
            reasoning = get_reasoning_delta(delta)
            if reasoning:
                yield NormalizedResponse(thinking_content=reasoning)
            for kind, text in parser.feed(getattr(delta, "content", None) or ""):
                yield NormalizedResponse(
                    **{
                        "thinking_content"
                        if kind == "thinking"
                        else "text_content": text
                    }
                )
        if choice.finish_reason is None:
            continue
        finished = True
        reason = normalize_stop(choice.finish_reason)
        for kind, text in parser.flush():
            yield NormalizedResponse(
                **{"thinking_content" if kind == "thinking" else "text_content": text}
            )
        if reason not in {"end_turn", "tool_calls"} or (
            reason == "end_turn" and buffers
        ):
            yield NormalizedResponse(
                stop_reason=reason, provider_error={"code": completion_error(reason)}
            )
            return
        calls = []
        if reason == "tool_calls":
            try:
                for buf in buffers.values():
                    if not buf["id"] or not buf["name"]:
                        raise ValueError("Incomplete tool identity")
                    calls.append(
                        NormalizedToolCall(
                            call_id=buf["id"],
                            tool_name=buf["name"],
                            arguments=strict_arguments(buf["arguments"]),
                        )
                    )
                if not calls or len({call.call_id for call in calls}) != len(calls):
                    raise ValueError("Invalid tool batch")
            except (ValueError, RecursionError):
                yield NormalizedResponse(
                    provider_error={"code": "PROVIDER_INVALID_TOOL_ARGUMENTS"}
                )
                return
        yield NormalizedResponse(tool_calls=calls, stop_reason=reason)
    if not finished:
        yield NormalizedResponse(
            stop_reason="unknown", provider_error={"code": "PROVIDER_INCOMPLETE"}
        )
