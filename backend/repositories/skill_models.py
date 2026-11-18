from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from core.clock import utcnow
from repositories.models import Base
from repositories.status_values import status_check


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[str] = mapped_column(Text)
    archive: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    uploaded_by: Mapped[str] = mapped_column(String(255))
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_skill_name_version"),
        status_check("skill_versions"),
    )


class SkillActivation(Base):
    __tablename__ = "skill_activations"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(36), ForeignKey("skill_versions.id"))
