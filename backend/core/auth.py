"""Bearer principals for deployments without an organization auth proxy.

Only SHA-256 token digests are configured. Identity/roles/scopes come from
server configuration, never request headers supplied by the client.
"""

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import json

from fastapi import HTTPException
from starlette.responses import JSONResponse
from core.config import settings


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]
    hosts: frozenset[str] = frozenset()
    services: frozenset[str] = frozenset()
    # Site/station scope for data capabilities: station ids or names, or "*".
    sites: frozenset[str] = frozenset()


LOCAL = Principal(
    "local", frozenset({"admin"}), frozenset({"*"}), frozenset({"*"}), frozenset({"*"})
)
_principal = ContextVar("principal", default=None)


def principal():
    actor = _principal.get()
    if actor is not None:
        if settings.auth_mode == "bearer":
            for entry in json.loads(settings.auth_principals_json):
                if entry["subject"] == actor.subject:
                    return Principal(
                        actor.subject,
                        frozenset(entry["roles"]),
                        frozenset(host.lower() for host in entry.get("hosts", [])),
                        frozenset(
                            service.lower() for service in entry.get("services", [])
                        ),
                        frozenset(site.lower() for site in entry.get("sites", [])),
                    )
            raise HTTPException(401, "Principal was revoked")
        return actor
    if settings.auth_mode == "local":
        return LOCAL
    raise HTTPException(401, "Authentication required")


def require(*roles):
    actor = principal()
    if "admin" not in actor.roles and not actor.roles.intersection(roles):
        raise HTTPException(403, "Insufficient permissions")
    return actor


def authenticate(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    for entry in json.loads(settings.auth_principals_json):
        if hmac.compare_digest(digest, entry["token_sha256"]):
            return Principal(
                entry["subject"],
                frozenset(entry["roles"]),
                frozenset(host.lower() for host in entry.get("hosts", [])),
                frozenset(service.lower() for service in entry.get("services", [])),
                frozenset(site.lower() for site in entry.get("sites", [])),
            )
    raise HTTPException(401, "Invalid bearer token")


class AuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope["path"].startswith("/api/")
            or scope["method"] == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return
        try:
            if settings.auth_mode == "local":
                actor = LOCAL
            else:
                headers = dict(scope["headers"])
                authorization = headers.get(b"authorization", b"").decode(
                    "ascii", errors="replace"
                )
                scheme, _, token = authorization.partition(" ")
                if scheme.lower() != "bearer" or not token or len(token) > 4096:
                    raise HTTPException(401, "Authentication required")
                actor = authenticate(token)
        except HTTPException as exc:
            await JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        context = _principal.set(actor)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(context)
