"""Quarantine-only archive validation. Never imports or executes uploaded code."""

import ast
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import stat
from typing import Literal
import zipfile
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, create_model

MAX_ARCHIVE = 2 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024


class InputField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: Literal["string", "integer", "number", "boolean"]
    required: bool = True


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^custom_[a-z][a-z0-9_]{0,55}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=40)
    description: str = Field(min_length=10, max_length=1000)
    entrypoint: str = Field(min_length=1, max_length=150)
    risk: Literal["read_only"] = "read_only"
    network: Literal["none"] = "none"
    input_fields: list[InputField] = Field(max_length=30)
    sample_input: dict
    timeout_seconds: int = Field(default=10, ge=1, le=30)

    def input_model(self):
        fields = {}
        for field in self.input_fields:
            if field.name in fields:
                raise ValueError("Duplicate input field")
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
            "SkillInput", __config__=ConfigDict(extra="forbid", strict=True), **fields
        )


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
            if (
                path.suffix.lower() not in {".py", ".json", ".md", ".txt"}
                or info.filename in files
            ):
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
    if "manifest.json" not in files:
        raise ValueError("manifest.json is required")
    manifest = Manifest.model_validate_json(files["manifest.json"])
    if manifest.entrypoint not in files or not manifest.entrypoint.endswith(".py"):
        raise ValueError("Declared Python entrypoint was not found")
    manifest.input_model().model_validate(manifest.sample_input)
    from core.redaction import redact

    for path, content in files.items():
        if redact(content) != content:
            raise ValueError("Potential credential in uploaded source")
        if path.endswith(".py"):
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    if any(
                        name.split(".")[0]
                        not in {
                            "json",
                            "sys",
                            "math",
                            "statistics",
                            "datetime",
                            "collections",
                            "re",
                        }
                        for name in names
                    ):
                        raise ValueError(
                            "Static review requires imports from the supported transformation modules only"
                        )
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id
                    in {"eval", "exec", "compile", "__import__", "open"}
                ):
                    raise ValueError("Static review rejected a restricted call")
    return manifest, files, hashlib.sha256(payload).hexdigest()
