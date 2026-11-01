from core.schemas import NormalizedMessage, NormalizedToolCall
import json

MAX_HISTORY_MESSAGES = 30


def normalize_call(call):
    function = call.get("function") or {}
    arguments = call.get("arguments", call.get("input", function.get("arguments", {})))
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    return NormalizedToolCall(call_id=call.get("id") or call.get("call_id", ""),
        tool_name=call.get("name") or call.get("tool_name") or function.get("name", ""), arguments=arguments)


def truncate_history(
    messages: list[NormalizedMessage],
    max_messages: int = MAX_HISTORY_MESSAGES,
) -> list[NormalizedMessage]:
    if len(messages) <= max_messages:
        return messages

    recent = messages[-max_messages:]

    while recent and recent[0].role in ("tool", "assistant"):
        recent = recent[1:]

    return recent


def build_messages(
    history: list[dict],
    user_message: str,
    system_prompt: str,
    append_user: bool = True,
) -> list[NormalizedMessage]:
    normalized_history = []
    for row in history:
        tool_calls = None
        if row.get("tool_calls"):
            tool_calls = [normalize_call(call) for call in row["tool_calls"]]
        
        normalized_history.append(
            NormalizedMessage(
                role=row["role"],
                content=row.get("content"),
                thinking_content=row.get("thinking_content"),
                tool_calls=tool_calls,
                tool_call_id=row.get("tool_call_id"),
                tool_name=row.get("name"),
                is_error=row.get("is_error", False),
                source_id=str(row["id"]) if row.get("id") is not None else None,
            )
        )

    system_msg = NormalizedMessage(role="system", content=system_prompt)
    recent = truncate_history(normalized_history)
    if not append_user:
        return [system_msg, *recent]

    user_msg = NormalizedMessage(role="user", content=user_message)
    return [system_msg, *recent, user_msg]
