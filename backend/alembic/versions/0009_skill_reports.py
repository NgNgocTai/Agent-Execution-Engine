"""Persist skill pipeline evidence without modifying existing artifacts."""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"


def upgrade():
    op.add_column(
        "skill_versions", sa.Column("validation_report", sa.Text(), nullable=True)
    )


def downgrade():
    raise RuntimeError("Skill review downgrade requires a backup/restore release")
