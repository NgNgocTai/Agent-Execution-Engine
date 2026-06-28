from core.schemas import NormalizedMessage, NormalizedToolCall

MAX_HISTORY_MESSAGES = 80


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
) -> list[NormalizedMessage]:
    normalized_history = []
    for row in history:
        tool_calls = None
        if row.get("tool_calls"):
            tool_calls = [
                NormalizedToolCall(
                    call_id=tc.get("id", tc.get("call_id", "")),
                    tool_name=tc.get("name", tc.get("tool_name", "")),
                    arguments=tc.get("arguments", tc.get("input", {})),
                )
                for tc in row["tool_calls"]
            ]
        
        normalized_history.append(
            NormalizedMessage(
                role=row["role"],
                content=row.get("content"),
                thinking_content=row.get("thinking_content"),
                tool_calls=tool_calls,
                tool_call_id=row.get("tool_call_id"),
                tool_name=row.get("name"),
                is_error=row.get("is_error", False),
            )
        )

    system_msg = NormalizedMessage(role="system", content=system_prompt)
    recent = truncate_history(normalized_history)
    user_msg = NormalizedMessage(role="user", content=user_message)
    return [system_msg, *recent, user_msg]
