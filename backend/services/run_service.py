from core.clock import utcnow
import hashlib
import json
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.auth import principal, require
from core.redaction import redact
from repositories.conversation_repo import ConversationRepository
from repositories.models import Message
from repositories.models import ActorAdmission
from core.config import settings
from repositories.run_models import Run, RunEvent, RunJob, RunStep
from core.request_context import step_identifier
from repositories.audit_repo import audit

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "unknown"}


async def get_owned(session, run_id):
    require("viewer", "operator", "approver", "skill_reviewer")
    run = await session.get(Run, run_id)
    if (
        run is None
        or await ConversationRepository(session).get(run.conversation_id) is None
    ):
        raise HTTPException(404, "Run not found")
    return run


async def append_event(session, run_id, event_type, payload):
    sequence = await session.scalar(
        update(Run)
        .where(Run.id == run_id)
        .values(
            sequence=Run.sequence + 1,
            updated_at=utcnow(),
        )
        .returning(Run.sequence)
    )
    if sequence is None:
        raise ValueError("Run not found")
    safe = redact(payload)
    safe.pop("new_messages", None)
    safe.pop("updated_messages", None)
    key = (
        safe.get("step_key")
        if event_type == "progress"
        else "tool:" + str(safe.get("call_id"))
        if event_type in {"tool_call_start", "tool_call_result", "approval_required"}
        else None
    )
    if key:
        step_id = step_identifier(run_id, key)
        step = await session.get(RunStep, step_id)
        if event_type == "progress":
            status = safe.get("status", "running")
        elif event_type == "tool_call_start":
            status = "running"
        elif event_type == "approval_required":
            status = "waiting_approval"
        else:
            status = "succeeded" if safe.get("status") == "success" else "failed"
        if step is None:
            step = RunStep(
                id=step_id,
                run_id=run_id,
                step_key=key,
                kind="llm" if key.startswith("llm:") else "tool",
                status=status,
                started_at=utcnow(),
            )
            session.add(step)
        step.status = status
        if status in {"succeeded", "failed"}:
            step.finished_at = utcnow()
        if safe.get("usage"):
            step.usage = json.dumps(safe["usage"])
        safe["step_id"] = step_id
    if event_type == "run_status" and safe.get("status") in TERMINAL:
        await session.execute(
            update(RunStep)
            .where(
                RunStep.run_id == run_id,
                RunStep.status.in_(["running", "waiting_approval"]),
            )
            .values(status=safe["status"], finished_at=utcnow())
        )
    record = RunEvent(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        payload=json.dumps(safe, ensure_ascii=False),
    )
    session.add(record)
    await session.flush()
    return record


def public_run(run):
    from services.run_configuration import configuration_summary
    return {
        "run_id": run.id,
        "conversation_id": run.conversation_id,
        "provider": run.provider,
        "skill": run.requested_skill or "auto",
        "configuration": configuration_summary(run.execution_config),
        "user_message_id": run.user_message_id,
        "status": run.status,
        "sequence": run.sequence,
        "error_code": run.error_code,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


async def create_run(
    session, *, message, provider, conversation_id, idempotency_key, skill=None
):
    actor = principal()
    from sqlalchemy import func

    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await session.execute(
        insert(ActorAdmission)
        .values(actor=actor.subject, touched_at=utcnow())
        .on_conflict_do_nothing(index_elements=["actor"])
    )
    await session.execute(
        update(ActorAdmission)
        .where(ActorAdmission.actor == actor.subject)
        .values(touched_at=utcnow())
    )
    # Auto-mode hashes stay identical to pre-selection runs for safe retries.
    canonical = json.dumps(
        [message, provider, conversation_id] + ([skill] if skill else []),
        ensure_ascii=False,
    )
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    existing = await session.scalar(
        select(Run).where(
            Run.actor == actor.subject, Run.idempotency_key == idempotency_key
        )
    )
    if existing:
        await get_owned(session, existing.id)
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Idempotency key was used for a different request")
        return existing
    active_count = await session.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.actor == actor.subject, Run.active_key.is_not(None))
    )
    if active_count >= settings.max_active_runs_per_actor:
        raise HTTPException(
            429, "Active run limit reached; wait, cancel or reconcile an existing run"
        )
    if skill:
        from services.skill_catalog import requestable_skill

        if not await requestable_skill(session, skill):
            raise HTTPException(422, "Requested skill is not available")
    from services.run_configuration import capture_configuration
    try:
        execution_config = capture_configuration(provider)
    except ValueError:
        raise HTTPException(422, "Invalid provider execution configuration") from None
    repo = ConversationRepository(session)
    if conversation_id:
        conversation = await repo.get(conversation_id, for_update=True)
        if not conversation:
            raise HTTPException(404, "Conversation not found")
        from repositories.models import PendingApproval

        uncertain = await session.scalar(
            select(PendingApproval.id)
            .where(
                PendingApproval.conversation_id == conversation_id,
                PendingApproval.status.in_(["executing", "rejecting", "unknown"]),
            )
            .limit(1)
        )
        if uncertain is not None:
            raise HTTPException(
                409, "Reconcile the existing action before starting another run"
            )
    else:
        conversation = await repo.create(provider, commit=False)
    run = Run(
        id=uuid4().hex,
        conversation_id=conversation.id,
        actor=actor.subject,
        provider=provider,
        requested_skill=skill,
        execution_config=execution_config,
        user_message=message,
        status="queued",
        active_key=conversation.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        sequence=0,
    )
    session.add(run)
    try:
        await session.flush()
        session.add(RunJob(run_id=run.id, status="queued", fencing_token=0))
        user_record = Message(
            conversation_id=conversation.id, role="user", content=message
        )
        session.add(user_record)
        await session.flush()
        run.user_message_id = user_record.id
        await append_event(session, run.id, "run_status", {"status": "queued"})
        await audit(session, "run.created", run.id)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Run).where(
                Run.actor == actor.subject, Run.idempotency_key == idempotency_key
            )
        )
        if existing and existing.request_hash == request_hash:
            await get_owned(session, existing.id)
            return existing
        raise HTTPException(
            409, "Conversation already has an active run or key conflict"
        ) from None
    return run


async def cancel_run(session, run_id):
    run = await get_owned(session, run_id)
    if run.status in TERMINAL:
        return run
    # Lock the job before reading execution claims; workers acquire this lock
    # before dispatch, so no new connector can claim after cancellation.
    await session.execute(
        update(RunJob)
        .where(RunJob.run_id == run_id)
        .values(
            status="cancelled",
            fencing_token=RunJob.fencing_token + 1,
            lease_until=None,
        )
    )
    from repositories.run_models import ToolExecution
    from repositories.models import PendingApproval

    write = await session.scalar(
        select(ToolExecution.id)
        .where(
            ToolExecution.run_id == run_id,
            ToolExecution.status == "running",
            ToolExecution.is_write.is_(True),
        )
        .limit(1)
    )
    approval = await session.scalar(
        select(PendingApproval.id)
        .where(
            PendingApproval.conversation_id == run.conversation_id,
            PendingApproval.status.in_(["executing", "unknown"]),
        )
        .limit(1)
    )
    status = "unknown" if write or approval else "cancelled"
    changed = await session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status.not_in(TERMINAL))
        .values(
            status=status,
            active_key=run.conversation_id if status == "unknown" else None,
            updated_at=utcnow(),
        )
    )
    if changed.rowcount:
        # Fence the worker before the cancellation event is committed.
        await session.execute(
            update(RunJob)
            .where(RunJob.run_id == run_id)
            .values(
                status="cancelled",
                fencing_token=RunJob.fencing_token + 1,
                lease_until=None,
            )
        )
        from repositories.models import PendingApproval

        await session.execute(
            update(PendingApproval)
            .where(
                PendingApproval.conversation_id == run.conversation_id,
                PendingApproval.status == "pending",
            )
            .values(status="cancelled")
        )
        await append_event(
            session,
            run_id,
            "run_status",
            {"status": status, "requires_reconciliation": status == "unknown"},
        )
        await audit(session, "run.cancelled", run_id)
        await session.commit()
    await session.refresh(run)
    return run
