"""Correlated model/tool steps and reported token usage."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"


def upgrade():
    op.create_table("run_steps", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_key", sa.String(150), nullable=False), sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("usage", sa.Text()),
        sa.Column("started_at", sa.DateTime(), nullable=False), sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("run_id", "step_key", name="uq_run_step_key"))
    op.create_index("ix_run_steps_run_id", "run_steps", ["run_id"])


def downgrade():
    raise RuntimeError("Step/audit downgrade requires a backup/restore release")
