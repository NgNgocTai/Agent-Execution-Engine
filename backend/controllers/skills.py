import asyncio
import base64
import json
from uuid import uuid4
import zipfile
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from core.auth import require
from core.redaction import redact
from repositories.engine import async_session
from repositories.skill_models import SkillVersion, SkillActivation
from repositories.audit_repo import audit
from services.skill_catalog import public_version, builtin_manifest
from services.skill_validation import (
    inspect_archive,
    MAX_ARCHIVE,
    SkillValidationError,
    Manifest,
)
from services.skill_pipeline import (
    initial_report,
    analyze,
    report_hash,
    manifest_hash,
    domain_passed,
    POLICY_VERSION,
    validation_slots,
)
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


async def read_package(request):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_ARCHIVE:
            raise HTTPException(413, "Skill ZIP exceeds the upload limit")
    try:
        async with validation_slots:
            manifest, files, digest = await asyncio.to_thread(
                inspect_archive, bytes(payload)
            )
    except SkillValidationError as exc:
        raise HTTPException(
            400, {"message": "Static skill validation failed", "report": exc.report}
        ) from None
    except (ValueError, zipfile.BadZipFile, UnicodeError, SyntaxError):
        raise HTTPException(
            400, "Skill ZIP failed manifest, source or archive validation"
        ) from None
    return bytes(payload), manifest, files, digest


@router.post("/inspect")
async def inspect_package(request: Request):
    require("skill_reviewer")
    _, manifest, files, digest = await read_package(request)
    return {
        "manifest": manifest.model_dump(),
        "content_hash": digest,
        "files": [
            {"path": path, "size": len(content.encode())}
            for path, content in files.items()
        ],
        "report": initial_report(manifest, files, digest),
    }


@router.post("", status_code=201)
async def upload(request: Request):
    actor = require("skill_reviewer")
    payload, manifest, files, digest = await read_package(request)
    record = SkillVersion(
        id=uuid4().hex,
        name=manifest.name,
        version=manifest.version,
        content_hash=digest,
        manifest=manifest.model_dump_json(),
        archive=base64.b64encode(payload).decode(),
        status="quarantined",
        uploaded_by=actor.subject,
        validation_report=json.dumps(initial_report(manifest, files, digest)),
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
    report_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("notes")
    @classmethod
    def substantive_notes(cls, value):
        value = value.strip()
        if len(value) < 10:
            raise ValueError(
                "Review notes must contain at least 10 non-padding characters"
            )
        return value


class Analyze(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    use_llm: bool = False
    provider: str = Field(default="openai", pattern=r"^(openai|anthropic|deepseek)$")


@router.post("/{version_id}/analyze")
async def analyze_version(version_id: str, req: Analyze):
    require("skill_reviewer")
    async with async_session() as session:
        record = await session.get(SkillVersion, version_id)
        if not record:
            raise HTTPException(404, "Skill version not found")
        if record.status != "quarantined" or record.content_hash != req.content_hash:
            raise HTTPException(
                409, "Only the matching quarantined artifact can be analyzed"
            )
        archive, old_report, old_manifest = (
            record.archive,
            record.validation_report,
            record.manifest,
        )
    manifest, files, digest = inspect_archive(base64.b64decode(archive))
    if digest != req.content_hash:
        raise HTTPException(409, "Archive hash changed")
    provider = None
    try:
        if req.use_llm:
            from core.config import settings

            if settings.data_mode == "fixture":
                raise HTTPException(422, "Online review is unavailable in fixture mode")
            from controllers.chat import _get_provider

            provider = _get_provider(req.provider)
            # This opt-in names one provider, so source is not sent through
            # the chat runtime's globally configured provider fallbacks.
            from services.providers.lifecycle import close_provider

            for fallback in getattr(provider, "fallbacks", []):
                await close_provider(fallback)
            provider.fallbacks = []
        manifest, report = await analyze(
            manifest, files, digest, runner=execute_sandbox, provider=provider
        )
    finally:
        if provider is not None:
            from services.providers.lifecycle import close_provider

            await close_provider(provider)
    from sqlalchemy import update

    require("skill_reviewer")
    async with async_session() as session:
        changed = await session.execute(
            update(SkillVersion)
            .where(
                SkillVersion.id == version_id,
                SkillVersion.status == "quarantined",
                SkillVersion.content_hash == digest,
                SkillVersion.validation_report == old_report,
                SkillVersion.manifest == old_manifest,
            )
            .values(
                manifest=manifest.model_dump_json(),
                validation_report=json.dumps(redact(report)),
            )
        )
        if not changed.rowcount:
            raise HTTPException(409, "Skill changed while validation was running")
        await audit(
            session,
            "skill.analyzed",
            version_id,
            {
                "hash": digest,
                "online": req.use_llm,
                "ready": report["ready_for_review"],
            },
        )
        await session.commit()
        return public_version(await session.get(SkillVersion, version_id))


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
        old_manifest, old_report = record.manifest, record.validation_report
        prior = json.loads(old_report) if old_report else None
        if prior and req.report_hash != report_hash(prior):
            raise HTTPException(
                409, "Inspect the latest validation report before reviewing"
            )
        report = prior
    if req.approved:
        try:
            _, files, digest = inspect_archive(base64.b64decode(archive))
            manifest = Manifest.model_validate_json(old_manifest)
            if digest != req.content_hash:
                raise ValueError("Hash mismatch")
            if prior and (
                prior.get("manifest_hash") != manifest_hash(manifest)
                or prior.get("policy_version") != POLICY_VERSION
            ):
                raise HTTPException(
                    409, "Re-analyze this artifact under the current validation policy"
                )
            manifest, report = await analyze(
                manifest, files, digest, runner=execute_sandbox
            )
            if prior:
                previous_llm = next(
                    (stage for stage in prior["stages"] if stage["stage"] == "llm"),
                    None,
                )
                if previous_llm and previous_llm["status"] == "passed":
                    report["stages"] = [
                        previous_llm if stage["stage"] == "llm" else stage
                        for stage in report["stages"]
                    ]
                    report["ready_for_review"] = domain_passed(report) and all(
                        entry["status"] == "passed" for entry in report["scripts"]
                    )
            if not report["ready_for_review"]:
                unavailable = any(
                    entry.get("error_code") == "SANDBOX_UNAVAILABLE"
                    for entry in report["scripts"]
                )
                # Save failed smoke evidence without approving the artifact.
                from sqlalchemy import update

                async with async_session() as session:
                    changed = await session.execute(
                        update(SkillVersion)
                        .where(
                            SkillVersion.id == version_id,
                            SkillVersion.status == "quarantined",
                            SkillVersion.validation_report == old_report,
                            SkillVersion.manifest == old_manifest,
                        )
                        .values(validation_report=json.dumps(redact(report)))
                    )
                    if not changed.rowcount:
                        raise HTTPException(409, "Skill changed while smoke testing")
                    await audit(
                        session, "skill.validation_failed", version_id, {"hash": digest}
                    )
                    await session.commit()
                raise HTTPException(
                    503 if unavailable else 422,
                    {
                        "message": "Skill validation has not passed",
                        "report": redact(report),
                    },
                )
        except HTTPException:
            raise
        except SandboxUnavailable:
            raise HTTPException(
                503, "Sandbox is unavailable; the version remains quarantined"
            ) from None
        except Exception:
            raise HTTPException(
                422, "Sandbox smoke test failed; the version remains quarantined"
            ) from None
    if report:
        for stage in report["stages"]:
            if stage["stage"] == "human":
                stage.update(
                    status="passed" if req.approved else "rejected",
                    reviewer=actor.subject,
                )
    require("skill_reviewer")
    async with async_session() as session:
        from sqlalchemy import update

        changed = await session.execute(
            update(SkillVersion)
            .where(
                SkillVersion.id == version_id,
                SkillVersion.status == "quarantined",
                SkillVersion.content_hash == req.content_hash,
                SkillVersion.validation_report == old_report,
                SkillVersion.manifest == old_manifest,
            )
            .values(
                status="approved" if req.approved else "rejected",
                reviewed_by=actor.subject,
                review_notes=redact(req.notes),
                validation_report=json.dumps(redact(report)) if report else None,
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
        from services.skill_catalog import resolved_manifest

        try:
            manifest, _ = resolved_manifest(record)
        except ValueError:
            raise HTTPException(
                409, "Artifact or run-spec no longer matches its validation policy"
            ) from None
        report = (
            json.loads(record.validation_report) if record.validation_report else None
        )
        if (
            not report
            or not report.get("ready_for_review")
            or not any(
                stage["stage"] == "human" and stage["status"] == "passed"
                for stage in report["stages"]
            )
        ):
            raise HTTPException(
                409, "A successful current pipeline and human review are required"
            )
        if manifest.run_specs() and not await sandbox_ready():
            raise HTTPException(503, "Reviewed sandbox image is unavailable")
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
