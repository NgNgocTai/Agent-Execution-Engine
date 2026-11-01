"""Redact copies at output boundaries; connector credentials remain intact."""

import logging
import re
import traceback
from pathlib import Path
from pydantic import BaseModel
from core.config import settings

MASK = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token$|credential|api[_-]?key|authorization|private[_-]?key|token_sha256",
    re.I,
)
ASSIGNMENT = re.compile(
    r"""(?i)\b(password|passwd|secret|[\w-]*token|api[_-]?key|private[_-]?key|authorization)\b(["']?\s*[:=]\s*["']?)([^\s,;"'}]+)"""
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S
)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
URL_PASSWORD = re.compile(r"(://[^:/\s]+:)[^@\s]+(@)")
QUOTED_ASSIGNMENT = re.compile(
    r"""(?i)(\b(?:password|passwd|secret|[\w-]*token|api[_-]?key|private[_-]?key|authorization)\b["']?\s*[:=]\s*)(["'])(?:\\.|(?!\2).)*?\2"""
)


def redact(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(key): MASK if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    for name in (
        "openai_api_key",
        "anthropic_api_key",
        "deepseek_api_key",
        "ssh_password",
        "clickhouse_password",
        "postgres_password",
        "langfuse_secret_key",
        "database_url",
    ):
        secret = getattr(settings, name, "")
        if secret and len(secret) >= 6:
            value = value.replace(secret, MASK)
    value = PRIVATE_KEY.sub(MASK, value)
    value = BEARER.sub("Bearer " + MASK, value)
    value = URL_PASSWORD.sub(r"\1" + MASK + r"\2", value)
    value = QUOTED_ASSIGNMENT.sub(
        lambda match: match[1] + match[2] + MASK + match[2], value
    )
    return ASSIGNMENT.sub(lambda match: match[1] + match[2] + MASK, value)


class RedactingFilter(logging.Filter):
    def filter(self, record):
        from core.request_context import request_id, run_id

        record.request_id, record.run_id = request_id.get(), run_id.get()
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            # Exception bodies can contain provider responses and credentials.
            record.msg += " [exception: " + record.exc_info[0].__name__ + "]"
            frames = traceback.extract_tb(record.exc_info[2])[-6:]
            record.msg += (
                " [frames: "
                + ", ".join(
                    f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                    for frame in frames
                )
                + "]"
            )
            record.exc_info = None
            record.exc_text = None
        return True
