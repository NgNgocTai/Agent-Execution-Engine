import json
from core.auth import principal
from core.redaction import redact
from repositories.models import AuditEntry
from core.request_context import request_id, run_id


async def audit(session, action, resource, details=None):
    session.add(AuditEntry(actor=principal().subject, action=action, resource=resource,
                           details=json.dumps(redact({**(details or {}), "request_id": request_id.get(), "run_id": run_id.get()}), ensure_ascii=False)))
    await session.flush()
