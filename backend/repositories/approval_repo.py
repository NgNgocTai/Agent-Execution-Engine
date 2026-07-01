import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.models import PendingApproval


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_pending(
        self,
        conversation_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict,
        approval_metadata: dict,
        expires_at: datetime,
    ) -> PendingApproval:
        approval = PendingApproval(
            conversation_id=conversation_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=json.dumps(arguments, ensure_ascii=False),
            approval_metadata=json.dumps(approval_metadata, ensure_ascii=False),
            status="pending",
            expires_at=expires_at,
        )
        self.session.add(approval)
        await self.session.commit()
        return approval

    async def get_pending(
        self,
        conversation_id: str,
        call_id: str,
    ) -> PendingApproval | None:
        result = await self.session.execute(
            select(PendingApproval).where(
                PendingApproval.conversation_id == conversation_id,
                PendingApproval.call_id == call_id,
                PendingApproval.status == "pending",
            )
        )
        return result.scalar_one_or_none()

    async def list_pending(self, conversation_id: str) -> list[dict]:
        now = _utcnow()
        result = await self.session.execute(
            select(PendingApproval)
            .where(
                PendingApproval.conversation_id == conversation_id,
                PendingApproval.status == "pending",
                PendingApproval.expires_at > now,
            )
            .order_by(PendingApproval.created_at)
        )
        return [self.to_dict(approval) for approval in result.scalars()]

    async def resolve(
        self,
        approval: PendingApproval,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> PendingApproval:
        approval.status = status
        approval.resolved_at = _utcnow()
        approval.result = json.dumps(result, ensure_ascii=False) if result is not None else None
        approval.error = error
        await self.session.commit()
        return approval

    @staticmethod
    def to_dict(approval: PendingApproval) -> dict:
        return {
            "id": approval.id,
            "conversation_id": approval.conversation_id,
            "call_id": approval.call_id,
            "tool_name": approval.tool_name,
            "arguments": json.loads(approval.arguments),
            "approval": json.loads(approval.approval_metadata),
            "status": approval.status,
            "created_at": approval.created_at.isoformat(),
            "expires_at": approval.expires_at.isoformat(),
        }
