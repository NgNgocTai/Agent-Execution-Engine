"""Ownership and independent audit trail."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column("conversations", sa.Column("owner_id", sa.String(255), nullable=False, server_default="local"))
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_table("audit_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(255), nullable=False), sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource", sa.String(255), nullable=False), sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_audit_entries_created_at", "audit_entries", ["created_at"])


def downgrade():
    raise RuntimeError("Ownership/audit downgrade is intentionally blocked")
