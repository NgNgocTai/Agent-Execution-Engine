"""Pin execution configuration; legacy runs deliberately remain unpinned."""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"


def upgrade():
    op.add_column("runs", sa.Column("execution_config", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("Run configuration downgrade requires backup/restore")
