"""Reviewed immutable transformation skills and run version pins."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"


def upgrade():
    op.add_column("runs", sa.Column("selected_skill_versions", sa.Text(), nullable=False, server_default="{}"))
    op.create_table("skill_versions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(40), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("manifest", sa.Text(), nullable=False), sa.Column("archive", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("uploaded_by", sa.String(255), nullable=False),
        sa.Column("reviewed_by", sa.String(255)), sa.Column("review_notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_skill_name_version"))
    op.create_table("skill_activations", sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("skill_versions.id"), nullable=False))


def downgrade():
    raise RuntimeError("Skill version downgrade requires an artifact retention review")
