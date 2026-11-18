"""Record an optional operator-selected skill; existing runs stay in auto mode."""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"


def upgrade():
    op.add_column("runs", sa.Column("requested_skill", sa.String(100), nullable=True))


def downgrade():
    raise RuntimeError("Requested skill downgrade requires backup/restore")
