"""Quarantine-only archive validation. Never imports or executes uploaded code."""

import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import stat
from typing import Annotated, Literal
import zipfile
import unicodedata

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    create_model,
    model_validator,
)
from services.skill_ast import analyze_python
from services.skill_schema import (
    MAX_VALUE_BYTES,
    SchemaError,
    public_schema,
    validate_schema,
    validate_value,
)

# .sh files are stored as inert text resources; only declared Python entrypoints
# ever execute. YAML is accepted solely as agents/<name>.yaml descriptive metadata.
TEXT_SUFFIXES = {".py", ".json", ".md", ".txt", ".csv", ".sh"}
METADATA_SUFFIXES = {".yaml", ".yml"}
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024


class InputField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: Literal["string", "integer", "number", "boolean", "object", "array"]
    required: bool = True
    shape: dict | None = None

    @model_validator(mode="after")
    def check_shape(self):
        if self.kind not in {"object", "array"}:
            if self.shape is not None:
                raise ValueError("Only object and array fields declare a shape")
            return self
        if self.shape is None:
            raise ValueError("Object and array fields require a shape")
        try:
            validate_schema(self.shape)
        except SchemaError as exc:
            raise ValueError(f"Invalid input shape: {exc}") from exc
        if self.shape["type"] != self.kind:
            raise ValueError("Input shape type must match the field kind")
        if self.kind == "object" and not self.required and self.shape.get("required"):
            raise ValueError("Optional objects cannot have required properties")
        return self


def _structured(field):
    shape = field.shape

    def check(value):
        validate_value(shape, value)
        return value

    base = dict if field.kind == "object" else list
    return Annotated[base, AfterValidator(check), WithJsonSchema(public_schema(shape))]


def _bounded_total(model):
    size = len(json.dumps(model.model_dump(), ensure_ascii=False).encode())
    if size > MAX_VALUE_BYTES:
        raise ValueError("Skill input is too large")
    return model


class ScriptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entrypoint: str = Field(min_length=1, max_length=150)
    purpose: str = Field(default="Transform supplied data", max_length=1000)
    input_fields: list[InputField] = Field(default_factory=list, max_length=30)
    sample_input: dict = Field(default_factory=dict)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    output_contract: Literal["skill_result", "json", "text"] = "skill_result"
    output_schema: dict | None = None

    @model_validator(mode="after")
    def check_output_schema(self):
        if self.output_schema is None:
            return self
        try:
            validate_schema(self.output_schema)
        except SchemaError as exc:
            raise ValueError(f"Invalid output schema: {exc}") from exc
        if self.output_contract == "text" and self.output_schema["type"] != "string":
            raise ValueError("A text output can only declare a string schema")
        return self

    def input_model(self):
        fields = {}
        for field in self.input_fields:
            if field.name in fields:
                raise ValueError("Duplicate input field")
            if field.kind in {"object", "array"}:
                fields[field.name] = (
                    _structured(field),
                    ...
                    if field.required
                    else Field(default_factory=dict if field.kind == "object" else list),
                )
                continue
            kind = {"string": str, "integer": int, "number": float, "boolean": bool}[
                field.kind
            ]
            if field.kind == "string":
                default = Field(... if field.required else "", max_length=10000)
            else:
                default = (
                    ... if field.required else False if field.kind == "boolean" else 0
                )
            fields[field.name] = (kind, default)
        return create_model(
            "SkillInput",
            __config__=ConfigDict(extra="forbid", strict=True, allow_inf_nan=False),
            __validators__={
                "bounded_total": model_validator(mode="after")(_bounded_total)
            },
            **fields,
        )


class Manifest(ScriptSpec):
    name: str = Field(pattern=r"^custom_[a-z][a-z0-9_]{0,55}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=40)
    description: str = Field(min_length=10, max_length=1000)
    entrypoint: str = Field(default="", max_length=150)
    risk: Literal["read_only"] = "read_only"
    network: Literal["none"] = "none"
    instructions: str = Field(default="", max_length=32768)
    package_format: Literal["manifest", "agent_skill"] = "manifest"
    scripts: list[ScriptSpec] = Field(default_factory=list, max_length=20)

    def run_specs(self):
        if self.scripts:
            return self.scripts
        if not self.entrypoint:
            return []
        return [
            ScriptSpec.model_validate(
                {key: getattr(self, key) for key in ScriptSpec.model_fields}
            )
        ]


class SkillValidationError(ValueError):
    def __init__(self, report):
        super().__init__("Skill package failed static validation")
        self.report = report


def validate_sources(files):
    from core.redaction import redact

    findings = []
    for path, content in files.items():
        if redact(content) != content:
            findings.append(
                {
                    "path": path,
                    "line": 1,
                    "code": "POTENTIAL_SECRET",
                    "message": "Potential credential in source",
                }
            )
        if path.endswith(".py"):
            findings.extend(analyze_python(content, path))
    report = {
        "stage": "static",
        "status": "failed" if findings else "passed",
        "findings": findings[:200],
    }
    if findings:
        raise SkillValidationError(report)
    return report


def inspect_archive(payload):
    if not payload or len(payload) > MAX_ARCHIVE:
        raise ValueError("Archive must be between 1 byte and 2 MiB")
    files, total, names = {}, 0, set()
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        if len(archive.infolist()) > 100:
            raise ValueError("Archive has too many files")
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            canonical = path.as_posix() + ("/" if info.is_dir() else "")
            if (
                not info.filename
                or info.orig_filename != info.filename
                or "\\" in info.filename
                or ":" in info.filename
                or "\x00" in info.filename
                or path.is_absolute()
                or any(part in {"..", "."} for part in path.parts)
                or info.filename.startswith("/")
                or info.flag_bits & 1
                or info.filename != canonical
                or info.filename != unicodedata.normalize("NFC", info.filename)
                or any(part.endswith((".", " ")) for part in path.parts)
            ):
                raise ValueError("Unsafe archive path or encrypted entry")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (
                stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
            ):
                raise ValueError("Links and special files are forbidden")
            if info.is_dir():
                continue
            if info.filename.casefold() in names:
                raise ValueError("Archive contains case-colliding paths")
            names.add(info.filename.casefold())
            suffix = path.suffix.lower()
            if (
                suffix not in TEXT_SUFFIXES
                and not (suffix in METADATA_SUFFIXES and path.parent.name == "agents")
            ) or info.filename in files:
                raise ValueError("Unsupported or duplicate archive entry")
            total += info.file_size
            if (
                total > MAX_EXPANDED
                or info.file_size > 2 * 1024 * 1024
                or info.file_size > max(info.compress_size, 1) * 100
            ):
                raise ValueError("Archive expansion limit exceeded")
            content = archive.read(info).decode("utf-8")
            files[info.filename] = content
    # A single wrapper directory is accepted without writing/extracting files.
    if "manifest.json" not in files and "SKILL.md" not in files:
        roots = {PurePosixPath(name).parts[0] for name in files}
        if len(roots) == 1 and all(
            len(PurePosixPath(name).parts) > 1 for name in files
        ):
            prefix = next(iter(roots)) + "/"
            files = {name[len(prefix) :]: value for name, value in files.items()}
    if "args.json" in files:
        raise ValueError("args.json is reserved for runtime arguments")
    from services.skill_markdown import agent_metadata

    agent_metadata(files)  # rejects misplaced or unsafe YAML metadata
    if "manifest.json" in files:
        manifest = Manifest.model_validate_json(files["manifest.json"])
    else:
        from services.skill_markdown import markdown_manifest

        manifest = markdown_manifest(files)
    specs = manifest.run_specs()
    if not specs and not manifest.instructions:
        raise ValueError("A skill needs instructions or a declared entrypoint")
    if len({spec.entrypoint for spec in specs}) != len(specs):
        raise ValueError("Duplicate script entrypoint")
    for spec in specs:
        if spec.entrypoint not in files or not spec.entrypoint.endswith(".py"):
            raise ValueError("Declared Python entrypoint was not found")
        spec.input_model().model_validate(spec.sample_input)
    validate_sources(files)
    return manifest, files, hashlib.sha256(payload).hexdigest()
