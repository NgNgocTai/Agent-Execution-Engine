import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.models import Conversation, Message
from core.auth import principal
from core.clock import utcnow


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, provider: str, *, commit: bool = True) -> Conversation:
        conv = Conversation(id=uuid4().hex, provider=provider, owner_id=principal().subject)
        self.session.add(conv)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return conv

    async def get(self, conversation_id: str, *, for_update: bool = False) -> Conversation | None:
        query = select(Conversation).where(Conversation.id == conversation_id, Conversation.deleted_at.is_(None))
        actor = principal()
        if "admin" not in actor.roles:
            query = query.where(Conversation.owner_id == actor.subject)
        if for_update:
            query = query.with_for_update()
        result = await self.session.execute(query)
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
        *, commit: bool = True,
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
            if commit:
                await self.session.commit()
            else:
                await self.session.flush()
        return records

    async def get_messages(self, conversation_id: str, limit: int = 500, before_id: int | None = None) -> list[dict]:
        query = select(Message).where(Message.conversation_id == conversation_id)
        if before_id is not None:
            query = query.where(Message.id < before_id)
        result = await self.session.execute(
            query.order_by(Message.id.desc()).limit(min(max(limit, 1), 500))
        )
        messages = []
        for msg in reversed(result.scalars().all()):
            entry: dict = {"id": msg.id, "created_at": msg.created_at.isoformat(), "role": msg.role, "is_error": msg.is_error}
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
        query = select(Conversation).where(Conversation.deleted_at.is_(None)).order_by(Conversation.created_at.desc(), Conversation.id)
        actor = principal()
        if "admin" not in actor.roles:
            query = query.where(Conversation.owner_id == actor.subject)
        result = await self.session.execute(query.limit(100))
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
        if await self.get(conversation_id, for_update=True) is None:
            return False
        active = await self.session.scalar(select(PendingApproval.id).where(
            PendingApproval.conversation_id == conversation_id,
            PendingApproval.status.in_(["executing", "rejecting", "unknown"]),
        ).limit(1))
        if active is not None:
            from fastapi import HTTPException
            raise HTTPException(409, "Conversation has an execution requiring reconciliation")
        
        from repositories.run_models import Run
        active_run = await self.session.scalar(select(Run.id).where(Run.conversation_id == conversation_id, Run.active_key.is_not(None)).limit(1))
        if active_run:
            from fastapi import HTTPException
            raise HTTPException(409, "Cancel or reconcile the active run before deleting this conversation")
        conversation = await self.get(conversation_id)
        conversation.deleted_at = utcnow()
        from repositories.audit_repo import audit
        await audit(self.session, "conversation.deleted", conversation_id)
        await self.session.commit()
        return True

    async def rename(self, conversation_id: str, new_title: str) -> bool:
        conv = await self.get(conversation_id)
        if not conv:
            return False
        conv.title = new_title
        await self.session.commit()
        return True
