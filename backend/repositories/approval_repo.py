import json
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.models import PendingApproval, RemediationGuard
from core.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ApprovalRepository:
    def __init__(self, session: AsyncSession, *, commit_on_create: bool = True) -> None:
        self.session = session
        self.commit_on_create = commit_on_create

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
        if self.commit_on_create:
            await self.session.commit()
        else:
            await self.session.flush()
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
        if approved:
            host = json.loads(approval.approval_metadata).get("host", "").lower()
            if self.session.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            elif self.session.bind.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise RuntimeError("Remediation claims require PostgreSQL or SQLite")
            created = await self.session.execute(insert(RemediationGuard).values(
                host=host, approval_id=approval.id, available_after=None,
            ).on_conflict_do_nothing(index_elements=["host"]))
            if not created.rowcount:
                renewed = await self.session.execute(update(RemediationGuard).where(
                    RemediationGuard.host == host, RemediationGuard.available_after <= _utcnow(),
                ).values(approval_id=approval.id, available_after=None))
                if not renewed.rowcount:
                    await self.session.rollback()
                    return False
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
        if claimed:
            await self.session.commit()
        else:
            await self.session.rollback()
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
        if changed and status == "approved":
            await self.session.execute(update(RemediationGuard).where(RemediationGuard.approval_id == approval.id).values(
                available_after=now + timedelta(seconds=settings.remediation_cooldown_seconds)))
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
