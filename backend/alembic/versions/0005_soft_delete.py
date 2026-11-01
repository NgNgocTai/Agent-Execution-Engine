"""Keep audit and side-effect history when a conversation is hidden."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"


def upgrade():
    op.add_column("conversations", sa.Column("deleted_at", sa.DateTime()))


def downgrade():
    raise RuntimeError("Soft delete downgrade requires a retention review")
