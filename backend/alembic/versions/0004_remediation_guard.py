"""Serialize remediation per host; retain uncertain executions for reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"


def upgrade():
    op.create_table("remediation_guards",
        sa.Column("host", sa.String(255), primary_key=True),
        sa.Column("approval_id", sa.Integer(), sa.ForeignKey("pending_approvals.id"), nullable=False),
        sa.Column("available_after", sa.DateTime()))


def downgrade():
    raise RuntimeError("Remediation guard downgrade requires reconciliation")
