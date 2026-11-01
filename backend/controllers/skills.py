import base64
import json
from uuid import uuid4
import zipfile
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from core.auth import require
from core.redaction import redact
from repositories.engine import async_session
from repositories.skill_models import SkillVersion, SkillActivation
from repositories.audit_repo import audit
from services.skill_catalog import public_version, builtin_manifest
from services.skill_validation import inspect_archive, MAX_ARCHIVE
from services.sandbox_executor import execute_sandbox, SandboxUnavailable, sandbox_ready

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
async def list_skills():
    require("viewer", "operator", "approver", "skill_reviewer")
    from controllers.chat import engine

    async with async_session() as session:
        versions = (
            await session.scalars(
                select(SkillVersion)
                .order_by(SkillVersion.created_at.desc(), SkillVersion.id)
                .limit(100)
            )
        ).all()
        active = (await session.scalars(select(SkillActivation))).all()
        return {
            "builtins": [
                builtin_manifest(engine.skill_registry.get(name))
                for name in engine.skill_registry.list_names()
            ]
            if engine
            else [],
            "versions": [public_version(record) for record in versions],
            "active": {record.name: record.version_id for record in active},
        }


@router.post("", status_code=201)
async def upload(request: Request):
    actor = require("skill_reviewer")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_ARCHIVE:
            raise HTTPException(413, "Skill ZIP exceeds the upload limit")
    try:
        manifest, _, digest = inspect_archive(bytes(payload))
    except (ValueError, zipfile.BadZipFile, UnicodeError, SyntaxError):
        raise HTTPException(
            400, "Skill ZIP failed manifest, source or archive validation"
        ) from None
    record = SkillVersion(
        id=uuid4().hex,
        name=manifest.name,
        version=manifest.version,
        content_hash=digest,
        manifest=manifest.model_dump_json(),
        archive=base64.b64encode(payload).decode(),
        status="quarantined",
        uploaded_by=actor.subject,
    )
    async with async_session() as session:
        session.add(record)
        try:
            await session.flush()
            await audit(session, "skill.uploaded", record.id, {"hash": digest})
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                409, "This name/version already exists; upload a new version"
            ) from None
        return public_version(record)


@router.get("/{version_id}/source")
async def source(version_id: str):
    require("skill_reviewer")
    async with async_session() as session:
        record = await session.get(SkillVersion, version_id)
        if not record:
            raise HTTPException(404, "Skill version not found")
        _, files, digest = inspect_archive(base64.b64decode(record.archive))
        if digest != record.content_hash:
            raise HTTPException(409, "Artifact hash does not match its record")
        return {"content_hash": digest, "files": redact(files)}


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool
    notes: str = Field(min_length=10, max_length=2000)


@router.post("/{version_id}/review")
async def review(version_id: str, req: Review):
    actor = require("skill_reviewer")
    async with async_session() as session:
        record = await session.get(SkillVersion, version_id)
        if not record:
            raise HTTPException(404, "Skill version not found")
        if record.status != "quarantined" or record.content_hash != req.content_hash:
            raise HTTPException(
                409, "Review no longer matches the quarantined artifact"
            )
        if record.uploaded_by == actor.subject:
            raise HTTPException(403, "A different reviewer must review this version")
        archive = record.archive
    if req.approved:
        try:
            manifest, files, digest = inspect_archive(base64.b64decode(archive))
            if digest != req.content_hash:
                raise ValueError("Hash mismatch")
            result = await execute_sandbox(manifest, files, manifest.sample_input)
            if not result.success:
                raise ValueError("Smoke test failed")
        except SandboxUnavailable:
            raise HTTPException(
                503, "Sandbox is unavailable; the version remains quarantined"
            ) from None
        except Exception:
            raise HTTPException(
                422, "Sandbox smoke test failed; the version remains quarantined"
            ) from None
    async with async_session() as session:
        from sqlalchemy import update

        changed = await session.execute(
            update(SkillVersion)
            .where(
                SkillVersion.id == version_id,
                SkillVersion.status == "quarantined",
                SkillVersion.content_hash == req.content_hash,
            )
            .values(
                status="approved" if req.approved else "rejected",
                reviewed_by=actor.subject,
                review_notes=redact(req.notes),
            )
        )
        if not changed.rowcount:
            raise HTTPException(409, "Skill review changed concurrently")
        await audit(
            session,
            "skill.reviewed",
            version_id,
            {"hash": req.content_hash, "approved": req.approved},
        )
        await session.commit()
        return public_version(await session.get(SkillVersion, version_id))


@router.post("/{version_id}/activate")
async def activate(version_id: str):
    require("admin")
    if not await sandbox_ready():
        raise HTTPException(503, "Reviewed sandbox image is unavailable")
    async with async_session() as session:
        # Lock the reviewed row so revoke/activate serialize on PostgreSQL.
        from sqlalchemy import update

        changed = await session.execute(
            update(SkillVersion)
            .where(SkillVersion.id == version_id, SkillVersion.status == "approved")
            .values(status="approved")
        )
        if not changed.rowcount:
            raise HTTPException(409, "Only approved versions can be activated")
        record = await session.get(SkillVersion, version_id)
        activation = await session.get(SkillActivation, record.name)
        if activation:
            activation.version_id = record.id
        else:
            session.add(SkillActivation(name=record.name, version_id=record.id))
        await audit(
            session, "skill.activated", record.id, {"hash": record.content_hash}
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "Activation changed concurrently") from None
        return {"active": record.id}


@router.post("/{version_id}/revoke")
async def revoke(version_id: str):
    require("admin")
    async with async_session() as session:
        record = await session.scalar(
            select(SkillVersion).where(SkillVersion.id == version_id).with_for_update()
        )
        if not record:
            raise HTTPException(404, "Skill version not found")
        record.status = "revoked"
        await session.execute(
            delete(SkillActivation).where(SkillActivation.version_id == version_id)
        )
        await audit(session, "skill.revoked", record.id)
        await session.commit()
        return {"revoked": record.id}
