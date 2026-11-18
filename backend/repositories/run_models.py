from core.clock import utcnow
from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column
from repositories.models import Base
from repositories.status_values import INTERVENTION, check_sql, status_check


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id"), index=True
    )
    actor: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(20))
    execution_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_message: Mapped[str] = mapped_column(Text)
    user_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    selected_skill_versions: Mapped[str] = mapped_column(Text, default="{}")
    requested_skill: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    active_key: Mapped[str | None] = mapped_column(
        String(36), unique=True, nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("actor", "idempotency_key", name="uq_run_actor_key"),
        status_check("runs"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunJob(Base):
    __tablename__ = "run_jobs"
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    __table_args__ = (status_check("run_jobs"),)


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"))
    call_id: Mapped[str] = mapped_column(String(100))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    is_write: Mapped[bool] = mapped_column(default=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_execution_run_call"),
        Index("ix_execution_run_status", "run_id", "status"),
        status_check("tool_executions"),
    )


class RunIntervention(Base):
    __tablename__ = "run_interventions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), index=True)
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        CheckConstraint(check_sql(INTERVENTION), name="ck_run_interventions_status"),
    )


class RunCheckpoint(Base):
    __tablename__ = "run_checkpoints"
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text)


class RunStep(Base):
    __tablename__ = "run_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "step_key", name="uq_run_step_key"),
        status_check("run_steps"),
    )
