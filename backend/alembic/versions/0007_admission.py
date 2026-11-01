"""Serialize actor admission checks across web workers."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"


def upgrade():
    op.create_table("actor_admission", sa.Column("actor", sa.String(255), primary_key=True),
                    sa.Column("touched_at", sa.DateTime(), nullable=False))


def downgrade():
    raise RuntimeError("Admission guard downgrade requires a stopped-writer release")
