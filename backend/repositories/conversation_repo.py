import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.models import Conversation, Message


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, provider: str) -> Conversation:
        conv = Conversation(id=uuid4().hex, provider=provider)
        self.session.add(conv)
        await self.session.commit()
        return conv

    async def get(self, conversation_id: str) -> Conversation | None:
        result = await self.session.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        return result.scalar_one_or_none()

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str | None = None,
        thinking_content: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        is_error: bool = False,
    ) -> Message:
        messages = await self.add_messages(
            conversation_id,
            [{
                "role": role,
                "content": content,
                "thinking_content": thinking_content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
                "name": name,
                "is_error": is_error,
            }],
        )
        return messages[0]

    async def add_messages(
        self,
        conversation_id: str,
        messages: list[dict],
    ) -> list[Message]:
        """Commit a new turn's messages together, preserving tool-call/result pairs."""
        records = []
        for message in messages:
            values = dict(message)
            tool_calls = values.get("tool_calls")
            values["tool_calls"] = (
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
            )
            records.append(Message(conversation_id=conversation_id, **values))

        if records:
            self.session.add_all(records)
            await self.session.commit()
        return records

    async def get_messages(self, conversation_id: str) -> list[dict]:
        result = await self.session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        messages = []
        for msg in result.scalars():
            entry: dict = {"role": msg.role, "is_error": msg.is_error}
            if msg.content is not None:
                entry["content"] = msg.content
            if msg.thinking_content is not None:
                entry["thinking_content"] = msg.thinking_content
            if msg.tool_calls is not None:
                entry["tool_calls"] = json.loads(msg.tool_calls)
            if msg.tool_call_id is not None:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.name is not None:
                entry["name"] = msg.name
            messages.append(entry)
        return messages

    async def list_all(self) -> list[dict]:
        result = await self.session.execute(
            select(Conversation).order_by(Conversation.created_at.desc())
        )
        conversations = []
        for conv in result.scalars():
            if conv.title:
                title = conv.title
            else:
                first_msg = await self.session.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id, Message.role == "user")
                    .order_by(Message.created_at)
                    .limit(1)
                )
                first = first_msg.scalar_one_or_none()
                title = first.content[:50] if first and first.content else "New chat"
            conversations.append({
                "id": conv.id,
                "provider": conv.provider,
                "title": title,
                "created_at": str(conv.created_at),
            })
        return conversations

    async def delete(self, conversation_id: str) -> bool:
        from sqlalchemy import delete
        from repositories.models import PendingApproval
        
        await self.session.execute(
            delete(PendingApproval).where(PendingApproval.conversation_id == conversation_id)
        )

        # Delete messages first due to foreign key
        await self.session.execute(
            delete(Message).where(Message.conversation_id == conversation_id)
        )
        
        # Delete conversation
        result = await self.session.execute(
            delete(Conversation).where(Conversation.id == conversation_id)
        )
        
        await self.session.commit()
        return result.rowcount > 0

    async def rename(self, conversation_id: str, new_title: str) -> bool:
        conv = await self.get(conversation_id)
        if not conv:
            return False
        conv.title = new_title
        await self.session.commit()
        return True
