"""Legacy control database schema; existing databases require explicit adoption."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade():
    op.create_table("conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_table("messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text()), sa.Column("thinking_content", sa.Text()),
        sa.Column("tool_calls", sa.Text()), sa.Column("tool_call_id", sa.String(100)),
        sa.Column("name", sa.String(100)), sa.Column("is_error", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("idx_messages_conversation", "messages", ["conversation_id", "created_at"])
    op.create_table("pending_approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("call_id", sa.String(100), nullable=False), sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False), sa.Column("approval_metadata", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False), sa.Column("resolved_at", sa.DateTime()),
        sa.Column("result", sa.Text()), sa.Column("error", sa.Text()),
        sa.UniqueConstraint("conversation_id", "call_id", name="uq_pending_approval_call"))
    op.create_index("idx_pending_approvals_conversation", "pending_approvals", ["conversation_id", "status"])


def downgrade():
    raise RuntimeError("Destructive downgrade requires a backup/restore release procedure")
