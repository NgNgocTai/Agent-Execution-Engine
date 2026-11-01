from core.clock import utcnow
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(255), default="local", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id")
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    thinking_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_error: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("idx_messages_conversation", "conversation_id", "created_at"),
    )


class PendingApproval(Base):
    __tablename__ = "pending_approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id")
    )
    call_id: Mapped[str] = mapped_column(String(100))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[str] = mapped_column(Text)
    approval_metadata: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "call_id", name="uq_pending_approval_call"),
        Index("idx_pending_approvals_conversation", "conversation_id", "status"),
    )


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(100))
    resource: Mapped[str] = mapped_column(String(255))
    details: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class RemediationGuard(Base):
    __tablename__ = "remediation_guards"
    host: Mapped[str] = mapped_column(String(255), primary_key=True)
    approval_id: Mapped[int] = mapped_column(ForeignKey("pending_approvals.id"))
    available_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ActorAdmission(Base):
    __tablename__ = "actor_admission"
    actor: Mapped[str] = mapped_column(String(255), primary_key=True)
    touched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# Register workflow tables in the shared metadata used by migrations/tests.
from repositories import run_models  # noqa: E402,F401
from repositories import skill_models  # noqa: E402,F401
