"""Operator interventions with their own delivery lifecycle."""

from alembic import op
import sqlalchemy as sa
from repositories.status_values import INTERVENTION, check_sql

revision = "0013"
down_revision = "0012"


def upgrade():
    op.create_table(
        "run_interventions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime()),
        sa.CheckConstraint(check_sql(INTERVENTION), name="ck_run_interventions_status"),
    )
    op.create_index("ix_run_interventions_run_id", "run_interventions", ["run_id"])


def downgrade():
    raise RuntimeError("Intervention downgrade requires backup/restore")
