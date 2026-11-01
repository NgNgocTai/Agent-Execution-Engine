import base64
import hashlib
import inspect
import json
from pathlib import Path

from sqlalchemy import select
from repositories.skill_models import SkillVersion, SkillActivation
from services.skills.base import BaseSkill
from services.skills.registry import SkillRegistry
from services.skill_validation import inspect_archive
from services.sandbox_executor import execute_sandbox


def builtin_manifest(skill):
    source = inspect.getsourcefile(type(skill))
    document_name = {
        "node_health_remediate": "NodeHealthAuto-Remediate",
        "noc_alarm_enrichment": "NocAlarmEnrichment",
        "device_inventory_lookup": "DeviceInventoryLookup",
        "alarm_statistics": "AlarmStatistics",
    }.get(skill.name)
    from core.config import ROOT_DIR, settings

    directory = (
        Path(settings.builtin_skills_directory)
        if settings.builtin_skills_directory
        else ROOT_DIR / "skills"
    )
    document = directory / document_name / "SKILL.md" if document_name else None
    instructions = (
        document.read_text(encoding="utf-8")[:32768]
        if document and document.is_file()
        else None
    )
    schema = skill.get_tool_schema().parameters
    content = (
        (Path(source).read_text(encoding="utf-8").encode() if source else b"")
        + json.dumps(schema, sort_keys=True).encode()
        + (instructions or "").encode()
    )
    digest = hashlib.sha256(content).hexdigest()
    return {
        "name": skill.name,
        "version": "1.0.0",
        "content_hash": digest,
        "description": skill.description,
        "input_schema": schema,
        "instructions": instructions,
        "risk": "approval_required"
        if skill.name == "node_health_remediate"
        else "read_only",
        "connector_permissions": {
            "node_health_remediate": ["ssh"],
            "alarm_statistics": ["alarms", "inventory"],
            "device_inventory_lookup": ["inventory", "alarms"],
            "noc_alarm_enrichment": ["alarms", "inventory"],
        }.get(skill.name, []),
        "evidence_contract": "source, retrieved_at, observed_at, entity, time_window_minutes, warnings",
    }


def public_version(record):
    return {
        "id": record.id,
        "name": record.name,
        "version": record.version,
        "content_hash": record.content_hash,
        "manifest": json.loads(record.manifest),
        "status": record.status,
        "uploaded_by": record.uploaded_by,
        "reviewed_by": record.reviewed_by,
        "review_notes": record.review_notes,
        "created_at": record.created_at.isoformat(),
    }


class ScriptSkill(BaseSkill):
    def __init__(self, record, sessions):
        self.version_id, self.digest, self.sessions = (
            record.id,
            record.content_hash,
            sessions,
        )
        self.manifest, _, digest = inspect_archive(base64.b64decode(record.archive))
        if digest != self.digest:
            raise ValueError("Skill content hash changed")
        self.input_model = self.manifest.input_model()

    @property
    def name(self):
        return self.manifest.name

    @property
    def description(self):
        return self.manifest.description

    @property
    def parameters_schema(self):
        return self.input_model.model_json_schema()

    async def execute(self, **arguments):
        async with self.sessions() as session:
            record = await session.get(SkillVersion, self.version_id)
            if (
                not record
                or record.status != "approved"
                or record.content_hash != self.digest
            ):
                raise ValueError("Pinned skill version was revoked or changed")
            manifest, files, digest = inspect_archive(base64.b64decode(record.archive))
        if digest != self.digest:
            raise ValueError("Skill archive no longer matches its review")
        return await execute_sandbox(manifest, files, arguments)


async def registry_for_run(base, session, sessions, run):
    registry = SkillRegistry()
    for name in base.list_names():
        registry.register(base.get(name))
    pins = json.loads(run.selected_skill_versions)
    current_builtin = {
        name: {
            "kind": "builtin",
            "hash": builtin_manifest(base.get(name))["content_hash"],
        }
        for name in base.list_names()
    }
    if pins and {name for name, pin in pins.items() if pin["kind"] == "builtin"} != set(
        current_builtin
    ):
        raise ValueError(
            "Built-in catalog changed; this run needs an explicit checkpoint migration"
        )
    if not pins:
        pins = dict(current_builtin)
        active = (
            await session.scalars(
                select(SkillVersion)
                .join(SkillActivation, SkillActivation.version_id == SkillVersion.id)
                .where(SkillVersion.status == "approved")
                .order_by(SkillVersion.name)
            )
        ).all()
        for record in active:
            pins[record.name] = {
                "kind": "script",
                "id": record.id,
                "hash": record.content_hash,
            }
        run.selected_skill_versions = json.dumps(pins)
        await session.flush()
    for name, pin in pins.items():
        if pin["kind"] == "builtin":
            if current_builtin.get(name) != pin:
                raise ValueError(
                    "Built-in skill changed since this run was accepted; explicit migration is required"
                )
        else:
            record = await session.get(SkillVersion, pin["id"])
            if (
                not record
                or record.status != "approved"
                or record.content_hash != pin["hash"]
            ):
                raise ValueError("Pinned skill version is no longer available")
            registry.register(ScriptSkill(record, sessions))
    return registry
