import json
from datetime import datetime, timezone

from sqlalchemy import select, update
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

    async def get(self, conversation_id: str, call_id: str) -> PendingApproval | None:
        result = await self.session.execute(
            select(PendingApproval)
            .where(
                PendingApproval.conversation_id == conversation_id,
                PendingApproval.call_id == call_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def claim_pending(self, approval: PendingApproval, *, approved: bool) -> bool:
        """Commit ownership before any remote side effect, including across workers."""
        result = await self.session.execute(
            update(PendingApproval)
            .where(
                PendingApproval.id == approval.id,
                PendingApproval.status == "pending",
                PendingApproval.expires_at > _utcnow(),
                # Bind the claim to the exact action the caller inspected.
                PendingApproval.approval_metadata == approval.approval_metadata,
                PendingApproval.tool_name == approval.tool_name,
            )
            .values(status="executing" if approved else "rejecting")
            .execution_options(synchronize_session="fetch")
        )
        claimed = result.rowcount == 1
        await self.session.commit()
        return claimed

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
        *,
        commit: bool = True,
    ) -> bool:
        expected_status = {
            "approved": "executing",
            "rejected": "rejecting",
            "unknown": "executing",
            "expired": "pending",
        }.get(status)
        if expected_status is None:
            raise ValueError(f"Unsupported approval resolution: {status}")

        now = _utcnow()
        conditions = [
            PendingApproval.id == approval.id,
            PendingApproval.status == expected_status,
        ]
        if status == "expired":
            conditions.append(PendingApproval.expires_at <= now)
        resolution = await self.session.execute(
            update(PendingApproval)
            .where(*conditions)
            .values(
                status=status,
                resolved_at=now,
                result=json.dumps(result, ensure_ascii=False) if result is not None else None,
                error=error,
            )
            .execution_options(synchronize_session="fetch")
        )
        changed = resolution.rowcount == 1
        if commit:
            await self.session.commit()
        return changed

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
