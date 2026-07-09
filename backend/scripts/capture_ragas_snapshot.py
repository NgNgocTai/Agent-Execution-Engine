from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from repositories.engine import async_session
from repositories.models import Conversation, Message

DEFAULT_OUTPUT = BACKEND_DIR / "evals" / "datasets" / "noc_agent_ragas_quality_cases.json"


def _json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _slug(value: str, max_len: int = 32) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").upper()
    return cleaned[:max_len] or "SNAPSHOT"


def _tool_call_id(call: dict[str, Any]) -> str:
    return str(call.get("call_id") or call.get("id") or "")


def _tool_call_name(call: dict[str, Any]) -> str:
    return str(call.get("tool_name") or call.get("name") or "")


def _tool_call_args(call: dict[str, Any]) -> dict[str, Any]:
    args = call.get("arguments", call.get("input", {}))
    return args if isinstance(args, dict) else {}


async def _load_conversation(conversation_id: str) -> tuple[Conversation, list[Message]]:
    async with async_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise SystemExit(f"Conversation not found: {conversation_id}")

        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        messages = list(result.scalars())
        return conversation, messages


async def _list_conversations(limit: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Conversation).order_by(Conversation.created_at.desc()).limit(limit)
        )
        conversations = list(result.scalars())

        for conv in conversations:
            first_msg = await session.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.role == "user")
                .order_by(Message.created_at, Message.id)
                .limit(1)
            )
            first = first_msg.scalar_one_or_none()
            title = conv.title or (first.content[:80] if first and first.content else "New chat")
            print(f"{conv.id} | provider={conv.provider} | {conv.created_at} | {title}")


async def _list_turns(conversation_id: str) -> None:
    conversation, messages = await _load_conversation(conversation_id)
    print(f"{conversation.id} | provider={conversation.provider} | {conversation.created_at}")
    turn_index = 0
    for idx, msg in enumerate(messages):
        if msg.role != "user":
            continue
        turn_index += 1
        next_user = next(
            (
                next_idx
                for next_idx in range(idx + 1, len(messages))
                if messages[next_idx].role == "user"
            ),
            len(messages),
        )
        segment = messages[idx:next_user]
        tool_names = [
            tool_msg.name or tool_msg.tool_call_id or "unknown_tool"
            for tool_msg in segment
            if tool_msg.role == "tool"
        ]
        final = _final_answer(segment)
        content = (msg.content or "").replace("\n", " ")
        print(
            f"{turn_index}. {content[:140]} | "
            f"tools={tool_names or '-'} | final={'yes' if final else 'no'}"
        )


async def _find_conversation_id_by_user_message(needle: str) -> str:
    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.role == "user", Message.content.ilike(f"%{needle}%"))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        message = result.scalar_one_or_none()
        if message is None:
            raise SystemExit(f"No user message contains: {needle}")
        return str(message.conversation_id)


def _select_turn(
    messages: list[Message],
    *,
    turn_index: int | None,
    user_message_contains: str | None,
) -> list[Message]:
    user_positions = [idx for idx, msg in enumerate(messages) if msg.role == "user"]
    if not user_positions:
        raise SystemExit("Conversation has no user messages.")

    candidates: list[int]
    if turn_index is not None:
        if turn_index == 0 or abs(turn_index) > len(user_positions):
            raise SystemExit(
                f"Invalid --turn-index {turn_index}; conversation has {len(user_positions)} user turns."
            )
        selected_position = user_positions[turn_index - 1] if turn_index > 0 else user_positions[turn_index]
        candidates = [selected_position]
    elif user_message_contains:
        needle = user_message_contains.lower()
        candidates = [
            pos
            for pos in user_positions
            if needle in (messages[pos].content or "").lower()
        ]
        candidates.reverse()
        if not candidates:
            raise SystemExit(f"No user turn contains: {user_message_contains!r}")
    else:
        candidates = list(reversed(user_positions))

    for start in candidates:
        next_users = [pos for pos in user_positions if pos > start]
        end = next_users[0] if next_users else len(messages)
        segment = messages[start:end]
        has_tool = any(msg.role == "tool" for msg in segment)
        has_final = _final_answer(segment) is not None
        if has_tool and has_final:
            return segment

    raise SystemExit(
        "Could not find a turn with both tool observations and final assistant answer."
    )


def _final_answer(segment: list[Message]) -> str | None:
    for msg in reversed(segment):
        if msg.role != "assistant" or not msg.content:
            continue
        if msg.tool_calls:
            continue
        return msg.content
    return None


def _extract_tool_results(segment: list[Message]) -> list[dict[str, Any]]:
    call_args_by_id: dict[str, dict[str, Any]] = {}
    call_name_by_id: dict[str, str] = {}

    for msg in segment:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for call in _json_loads(msg.tool_calls, []):
            if not isinstance(call, dict):
                continue
            call_id = _tool_call_id(call)
            if not call_id:
                continue
            call_args_by_id[call_id] = _tool_call_args(call)
            call_name_by_id[call_id] = _tool_call_name(call)

    tool_results: list[dict[str, Any]] = []
    for msg in segment:
        if msg.role != "tool":
            continue

        parsed = _json_loads(msg.content, msg.content)
        call_id = msg.tool_call_id or f"tool-{len(tool_results) + 1}"
        tool_name = msg.name or call_name_by_id.get(call_id) or "unknown_tool"
        entry: dict[str, Any] = {
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments": call_args_by_id.get(call_id, {}),
            "status": "error" if msg.is_error else "success",
        }
        if msg.is_error:
            entry["error"] = parsed
            entry["data"] = None
        else:
            entry["data"] = parsed
        tool_results.append(entry)

    return tool_results


def _build_case(
    *,
    conversation: Conversation,
    segment: list[Message],
    case_id: str | None,
    name: str | None,
    model: str | None,
    comparison_group: str | None,
    reference_answer: str | None,
    source: str,
    snapshot_version: str,
    split: str,
    quality_scope: str | None,
) -> dict[str, Any]:
    user_message = next((msg.content for msg in segment if msg.role == "user"), None)
    if not user_message:
        raise SystemExit("Selected turn has no user message.")

    final_answer = _final_answer(segment)
    if not final_answer:
        raise SystemExit("Selected turn has no final assistant answer.")

    tool_results = _extract_tool_results(segment)
    if not tool_results:
        raise SystemExit("Selected turn has no tool messages.")

    captured_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    generated_case_id = case_id or f"RAGAS-{_slug(conversation.provider)}-{_slug(user_message, 20)}"

    case = {
        "id": generated_case_id,
        "name": name or user_message[:80],
        "type": "ragas_quality",
        "source": source,
        "quality_scope": quality_scope,
        "split": split,
        "snapshot_version": snapshot_version,
        "captured_at": captured_at,
        "conversation_id": conversation.id,
        "provider": conversation.provider,
        "model": model or "unknown",
        "comparison_group": comparison_group or generated_case_id,
        "user_input": user_message,
        "tool_results": tool_results,
        "final_answer": final_answer,
    }
    if reference_answer:
        case["expected"] = {"answer": {"reference_answer": reference_answer}}
    return case


def _append_case(path: Path, case: dict[str, Any], *, overwrite: bool) -> None:
    existing = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            raise SystemExit(f"Dataset is not a JSON list: {path}")

    ids = [item.get("id") for item in existing if isinstance(item, dict)]
    if case["id"] in ids:
        if not overwrite:
            raise SystemExit(
                f"Case id already exists: {case['id']}. Use --overwrite to replace it."
            )
        existing = [item for item in existing if item.get("id") != case["id"]]

    existing.append(case)
    _json_write(path, existing)


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture a frozen Ragas quality snapshot from a persisted conversation."
    )
    parser.add_argument("--list-conversations", action="store_true")
    parser.add_argument("--list-turns", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--conversation-id")
    parser.add_argument("--turn-index", type=int)
    parser.add_argument("--user-message-contains")
    parser.add_argument("--case-id")
    parser.add_argument("--name")
    parser.add_argument("--model")
    parser.add_argument("--comparison-group")
    parser.add_argument("--reference-answer")
    parser.add_argument("--source", default="real_conversation_snapshot")
    parser.add_argument("--snapshot-version", default="v1")
    parser.add_argument("--split", choices=("tune", "holdout"), default="tune")
    parser.add_argument(
        "--quality-scope",
        choices=("answer_quality", "rule_based", "needs_regeneration"),
        default="answer_quality",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.list_conversations:
        await _list_conversations(args.limit)
        return 0

    if args.list_turns:
        if not args.conversation_id:
            raise SystemExit("Missing --conversation-id for --list-turns.")
        await _list_turns(args.conversation_id)
        return 0

    if not args.conversation_id:
        if not args.user_message_contains:
            raise SystemExit("Missing --conversation-id. Use --list-conversations first.")
        args.conversation_id = await _find_conversation_id_by_user_message(
            args.user_message_contains
        )

    conversation, messages = await _load_conversation(args.conversation_id)
    segment = _select_turn(
        messages,
        turn_index=args.turn_index,
        user_message_contains=args.user_message_contains,
    )
    case = _build_case(
        conversation=conversation,
        segment=segment,
        case_id=args.case_id,
        name=args.name,
        model=args.model,
        comparison_group=args.comparison_group,
        reference_answer=args.reference_answer,
        source=args.source,
        snapshot_version=args.snapshot_version,
        split=args.split,
        quality_scope=args.quality_scope,
    )

    if args.dry_run:
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    output = Path(args.output)
    if not output.is_absolute():
        output = BACKEND_DIR / output
    _append_case(output, case, overwrite=args.overwrite)
    print(f"Captured snapshot: {case['id']}")
    print(f"Dataset updated: {output}")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
