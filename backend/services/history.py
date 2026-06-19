MAX_HISTORY_MESSAGES = 80


def truncate_history(
    messages: list[dict],
    max_messages: int = MAX_HISTORY_MESSAGES,
) -> list[dict]:
    if len(messages) <= max_messages:
        return messages

    recent = messages[-max_messages:]

    while recent and recent[0].get("role") in ("tool", "assistant"):
        recent = recent[1:]

    return recent


def build_messages(
    history: list[dict],
    user_message: str,
    system_prompt: str,
) -> list[dict]:
    system_msg = {"role": "system", "content": system_prompt}
    recent = truncate_history(history)
    user_msg = {"role": "user", "content": user_message}
    return [system_msg, *recent, user_msg]
