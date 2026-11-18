import asyncio
import base64
import hashlib
import inspect
import json
from pathlib import Path

from sqlalchemy import select
from repositories.skill_models import SkillVersion, SkillActivation
from services.skills.base import BaseSkill
from services.skills.registry import SkillRegistry
from services.skill_validation import inspect_archive, Manifest
from services.sandbox_executor import execute_sandbox
from services.skill_pipeline import manifest_hash
from services.skill_guides import guide_tools

BUILTIN_SKILL_NAMES = (
    "noc_alarm_enrichment",
    "device_inventory_lookup",
    "alarm_statistics",
    "node_health_remediate",
)
SHORT_DESCRIPTION = 600


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
    from services.skill_pipeline import report_hash

    report = json.loads(record.validation_report) if record.validation_report else None
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
        "validation_report": report,
        "report_hash": report_hash(report) if report else None,
        "created_at": record.created_at.isoformat(),
    }


def resolved_manifest(record):
    from services.skill_pipeline import manifest_hash, POLICY_VERSION

    original, files, digest = inspect_archive(base64.b64decode(record.archive))
    manifest = Manifest.model_validate_json(record.manifest)
    if digest != record.content_hash or (
        manifest.name,
        manifest.version,
        manifest.instructions,
    ) != (original.name, original.version, original.instructions):
        raise ValueError("Skill content or identity changed")
    if {spec.entrypoint for spec in manifest.run_specs()} != {
        spec.entrypoint for spec in original.run_specs()
    }:
        raise ValueError("Resolved run-spec refers to undeclared scripts")
    for spec in manifest.run_specs():
        spec.input_model().model_validate(spec.sample_input)
    if record.validation_report:
        report = json.loads(record.validation_report)
        if (
            report.get("manifest_hash") != manifest_hash(manifest)
            or report.get("policy_version") != POLICY_VERSION
        ):
            raise ValueError("Run-spec does not match its validation report")
    return manifest, files


class ScriptSkill(BaseSkill):
    def __init__(self, record, sessions, script_path=None, naming_version=2):
        self.version_id, self.digest, self.sessions = (
            record.id,
            record.content_hash,
            sessions,
        )
        self.manifest, _ = resolved_manifest(record)
        self.package_name = self.manifest.name
        from services.skill_pipeline import manifest_hash

        self.spec_digest = manifest_hash(self.manifest)
        self.script_path = script_path
        specs = self.manifest.run_specs()
        if script_path is None and len(specs) == 1:
            script_path = specs[0].entrypoint
            self.script_path = script_path
        self.tool_name = self.manifest.name
        if len(specs) > 1:
            if script_path is None:
                raise ValueError("Select a script from this multi-script package")
            identity = self.tool_name + "\0" + script_path if naming_version == 2 and len(self.tool_name) > 54 else script_path
            self.tool_name = (self.tool_name[:54] if naming_version == 2 else self.tool_name) + (
                "__" + hashlib.sha256(identity.encode()).hexdigest()[:8]
            )
        if script_path:
            spec = next(
                (spec for spec in specs if spec.entrypoint == script_path), None
            )
            if spec is None:
                raise ValueError("Script is not part of the reviewed package")
            self.manifest = Manifest.model_validate(
                {**self.manifest.model_dump(), **spec.model_dump(), "scripts": []}
            )
        self.input_model = self.manifest.input_model()

    @property
    def name(self):
        return self.tool_name

    @property
    def description(self):
        # Metadata only. Full SKILL.md and references are read on demand with
        # load_skill/read_skill_file, so they are not cut off or duplicated here.
        text = (self.manifest.description + " " + self.manifest.purpose)[
            :SHORT_DESCRIPTION
        ]
        if self.manifest.instructions:
            text += f" Call load_skill('{self.package_name}') for full instructions."
        return text

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
            manifest, files = resolved_manifest(record)
        from services.skill_pipeline import manifest_hash

        if manifest_hash(manifest) != self.spec_digest:
            raise ValueError("Reviewed run-spec changed since this run selected it")
        if not manifest.run_specs():
            from services.skills.base import SkillResult, SkillStatus

            return SkillResult(
                status=SkillStatus.SUCCESS, data={"instructions": manifest.instructions}
            )
        return await execute_sandbox(self.manifest, files, arguments)


async def requestable_skill(session, name):
    """A skill an operator may select: built-in or currently approved and active."""
    if name in BUILTIN_SKILL_NAMES:
        return True
    return (
        await session.scalar(
            select(SkillVersion.id)
            .join(SkillActivation, SkillActivation.version_id == SkillVersion.id)
            .where(SkillVersion.status == "approved", SkillVersion.name == name)
            .limit(1)
        )
        is not None
    )


def _builtin_loader(skill, pin):
    async def load():
        manifest = await asyncio.to_thread(builtin_manifest, skill)
        if manifest["content_hash"] != pin["hash"]:
            raise ValueError("Built-in skill changed since this run was accepted")
        text = manifest["instructions"] or (
            skill.description
            + "\n\nInput schema:\n"
            + json.dumps(manifest["input_schema"], ensure_ascii=False)
        )
        return text, {"SKILL.md": text}

    return load


def _custom_loader(sessions, pin):
    async def load():
        async with sessions() as session:
            record = await session.get(SkillVersion, pin["id"])
            if (
                not record
                or record.status != "approved"
                or record.content_hash != pin["hash"]
            ):
                raise ValueError("Pinned skill version was revoked or changed")
            manifest, files = resolved_manifest(record)
        if pin.get("spec_hash") and pin["spec_hash"] != manifest_hash(manifest):
            raise ValueError("Pinned skill run-spec changed")
        # Text files only: inspect_archive already limits packages to text types.
        return manifest.instructions, dict(files)

    return load


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
                "spec_hash": manifest_hash(
                    Manifest.model_validate_json(record.manifest)
                ),
                "naming_version": 2,
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
            manifest, _ = resolved_manifest(record)
            if pin.get("spec_hash") and pin["spec_hash"] != manifest_hash(manifest):
                raise ValueError("Pinned skill run-spec changed")
            specs = manifest.run_specs()
            for path in [spec.entrypoint for spec in specs] or [None]:
                skill = ScriptSkill(record, sessions, path, pin.get("naming_version", 1))
                if len(skill.name) > 64:
                    from services.workflow.state import CheckpointIncompatible
                    raise CheckpointIncompatible("Legacy tool names exceed the provider limit; start a new run")
                registry.register(skill)
    loaders = {}
    for name, pin in pins.items():
        loaders[name] = (
            _builtin_loader(base.get(name), pin)
            if pin["kind"] == "builtin"
            else _custom_loader(sessions, pin)
        )
    requested = getattr(run, "requested_skill", None)
    if requested:
        # Specific-skill runs only see that package's tools plus the readers.
        if requested not in loaders:
            raise ValueError("Requested skill is no longer available")
        narrowed = SkillRegistry()
        for name in registry.list_names():
            skill = registry.get(name)
            if getattr(skill, "package_name", skill.name) == requested:
                narrowed.register(skill)
        registry = narrowed
        loaders = {requested: loaders[requested]}
    for tool in guide_tools(loaders):
        registry.register(tool)
    return registry
