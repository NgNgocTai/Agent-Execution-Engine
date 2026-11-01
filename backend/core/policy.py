"""Server policy: approval can never widen the configured target scope."""

import shlex
import re
from enum import Enum

from core.config import settings
from core.auth import principal


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


def _entries(value):
    return {entry.strip().lower() for entry in value.split(",") if entry.strip()}


def target_allowed(host, service="", *, write=False):
    actor = principal()
    if "admin" not in actor.roles:
        if str(host).lower() not in actor.hosts and "*" not in actor.hosts:
            return False
        if (
            service
            and service.lower() not in actor.services
            and "*" not in actor.services
        ):
            return False
    hosts = _entries(settings.ssh_allowed_hosts)
    if hosts and str(host).lower() not in hosts:
        return False
    if write and not hosts:
        return False
    services = _entries(settings.ssh_allowed_services)
    if service and (services or write) and service.lower() not in services:
        return False
    return True


def plan(tool_name, arguments):
    actor = principal()
    if not actor.roles.intersection({"operator", "admin"}):
        return Decision.DENY
    if tool_name != "node_health_remediate":
        # Aggregate inventory/alarm connectors do not yet expose a row-scope
        # filter. Deny restricted principals instead of leaking other sites.
        if "admin" not in actor.roles and "*" not in actor.hosts:
            return Decision.DENY
        return Decision.ALLOW
    write = arguments.get("dry_run", True) is False
    if not target_allowed(
        arguments.get("host", ""), arguments.get("service", ""), write=write
    ):
        return Decision.DENY
    return Decision.REQUIRE_APPROVAL if write else Decision.ALLOW


def approved_snapshot(metadata):
    if metadata.get("skill_hash") or settings.deployment_mode == "production":
        from services.skill_catalog import builtin_manifest
        from services.skills.node_health_remediate import NodeHealthRemediateSkill

        if (
            metadata.get("skill_hash")
            != builtin_manifest(NodeHealthRemediateSkill())["content_hash"]
        ):
            return False
    host, service = metadata.get("host", ""), metadata.get("service", "")
    action = metadata.get("action")
    if action == "restart_service":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", service):
            return False
        expected = f"docker restart {shlex.quote(service)}" if service else None
    elif action == "restart_docker" and settings.allow_docker_restart:
        expected = "sudo -n systemctl restart docker"
    else:
        expected = None
    return bool(
        expected
        and metadata.get("command") == expected
        and target_allowed(host, service, write=True)
    )
