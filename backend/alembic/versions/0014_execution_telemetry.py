"""Timing and skill-version attribution for tool executions (nullable: old rows stay valid)."""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"


def upgrade():
    with op.batch_alter_table("tool_executions") as batch:
        batch.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("duration_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("skill_ref", sa.String(64), nullable=True))


def downgrade():
    raise RuntimeError("Telemetry downgrade requires backup/restore")
