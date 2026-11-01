"""Durable runs, events, job leases, executions and checkpoints."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_table("runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False), sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("user_message_id", sa.Integer(), sa.ForeignKey("messages.id")),
        sa.Column("active_key", sa.String(36), unique=True), sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("actor", "idempotency_key", name="uq_run_actor_key"))
    op.create_index("ix_runs_conversation_id", "runs", ["conversation_id"])
    op.create_table("run_events",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True), sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_table("run_jobs",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime()), sa.Column("worker_id", sa.String(100)))
    op.create_index("ix_run_jobs_status", "run_jobs", ["status"])
    op.create_table("tool_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("call_id", sa.String(100), nullable=False), sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("is_write", sa.Boolean(), nullable=False), sa.Column("result", sa.Text()),
        sa.UniqueConstraint("run_id", "call_id", name="uq_execution_run_call"))
    op.create_index("ix_execution_run_status", "tool_executions", ["run_id", "status"])
    op.create_table("run_checkpoints",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("state", sa.Text(), nullable=False))


def downgrade():
    raise RuntimeError("Run history downgrade requires a verified backup/restore")
