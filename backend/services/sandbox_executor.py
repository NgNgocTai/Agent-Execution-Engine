"""Reviewed scripts execute only in constrained Docker containers.

Artifacts travel over Docker's archive API, so a remote isolated daemon does
not need access to the worker's filesystem. No host Python fallback exists.
"""

import asyncio
from io import BytesIO
import json
import os
from pathlib import PurePosixPath
import re
import tarfile
from uuid import uuid4
from core.config import settings
from services.skills.base import SkillResult


class SandboxUnavailable(RuntimeError):
    pass


async def bounded_read(stream, maximum=65536):
    result, truncated = bytearray(), False
    while chunk := await stream.read(32768):
        remaining = maximum - len(result)
        result.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return bytes(result), truncated


async def docker_command(arguments, payload=None, timeout=5):
    process = await asyncio.create_subprocess_exec(
        "docker",
        *arguments,
        stdin=asyncio.subprocess.PIPE
        if payload is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )

    async def communicate():
        if payload is not None:
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
        output, error = await asyncio.gather(
            bounded_read(process.stdout), bounded_read(process.stderr)
        )
        await process.wait()
        return output, error

    try:
        output, error = await asyncio.wait_for(communicate(), timeout)
        return process.returncode, output, error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def pinned_image():
    image = settings.sandbox_image
    if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise SandboxUnavailable(
            "Configure a reviewed sandbox image by immutable digest"
        )
    return image


async def sandbox_ready():
    try:
        code, _, _ = await docker_command(["image", "inspect", pinned_image()])
        return code == 0
    except (OSError, SandboxUnavailable, TimeoutError):
        return False


def artifact_tar(files):
    output, directories = BytesIO(), set()
    for relative in files:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError("Unsafe artifact path")
        directories.update(
            parent.as_posix() for parent in path.parents if parent.as_posix() != "."
        )
    with tarfile.open(fileobj=output, mode="w") as archive:
        for directory in sorted(
            directories, key=lambda value: (value.count("/"), value)
        ):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode, info.uid, info.gid = 0o755, 65532, 65532
            archive.addfile(info)
        for relative, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(relative)
            info.mode, info.uid, info.gid, info.size = 0o644, 65532, 65532, len(data)
            archive.addfile(info, BytesIO(data))
    return output.getvalue()


async def execute_sandbox(manifest, files, arguments):
    image = pinned_image()
    name = "noc-skill-" + uuid4().hex
    staging, volume = name + "-stage", name + "-code"
    command = [
        "create",
        "--pull=never",
        "--name",
        name,
        "--label",
        "noc.skill.sandbox=true",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65532:65532",
        "--workdir",
        "/skill",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "128m",
        "--cpus",
        "0.5",
        "--ulimit",
        "nofile=128:128",
        "--ulimit",
        f"cpu={manifest.timeout_seconds}:{manifest.timeout_seconds}",
        "--mount",
        f"type=volume,source={volume},target=/skill,readonly",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--interactive",
    ]
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        command += ["--env", variable + "="]
    command += [image, "python", "-I", "-B", "/skill/" + manifest.entrypoint]
    try:
        async with asyncio.timeout(manifest.timeout_seconds):
            code, _, _ = await docker_command(
                ["volume", "create", "--label", "noc.skill.sandbox=true", volume]
            )
            if code != 0:
                raise SandboxUnavailable("Sandbox artifact volume is unavailable")
            # Never start the transfer container. Only the reviewed runtime
            # container receives stdin and executes code, with this volume RO.
            code, _, _ = await docker_command(
                [
                    "create",
                    "--pull=never",
                    "--name",
                    staging,
                    "--label",
                    "noc.skill.sandbox=true",
                    "--network",
                    "none",
                    "--read-only",
                    "--mount",
                    f"type=volume,source={volume},target=/skill",
                    image,
                    "true",
                ]
            )
            if code != 0:
                raise SandboxUnavailable("Sandbox artifact preparation failed")
            code, _, _ = await docker_command(
                ["cp", "-", staging + ":/skill"], artifact_tar(files)
            )
            if code != 0:
                raise SandboxUnavailable("Sandbox artifact transfer failed")
            await docker_command(["rm", staging])
            code, _, _ = await docker_command(command)
            if code != 0:
                raise SandboxUnavailable(
                    "Sandbox creation failed; host execution is disabled"
                )
            code, (stdout, truncated), (_, stderr_truncated) = await docker_command(
                ["start", "--attach", "--interactive", name],
                json.dumps(arguments).encode(),
                timeout=manifest.timeout_seconds,
            )
            if code != 0 or truncated or stderr_truncated:
                raise ValueError("Sandbox failed or exceeded its output limit")
            result = SkillResult.model_validate_json(stdout)
            if result.needs_approval or result.approval_metadata:
                raise ValueError(
                    "Transformation scripts cannot request privileged execution"
                )
            return result
    except FileNotFoundError as exc:
        raise SandboxUnavailable(
            "Docker is unavailable; script execution was denied"
        ) from exc
    finally:
        try:
            await docker_command(["rm", "--force", name, staging])
        except (OSError, TimeoutError):
            pass
        try:
            await docker_command(["volume", "rm", volume])
        except (OSError, TimeoutError):
            pass
