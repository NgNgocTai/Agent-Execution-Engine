from core.clock import utcnow

"""Database job ownership and fencing. No automatic retry of uncertain work."""
from datetime import timedelta
import json
import time
from sqlalchemy import select, update, exists

from repositories.run_models import Run, RunJob, ToolExecution
from services.run_service import TERMINAL, append_event
from services.skills.base import SkillResult
from core.request_context import step_identifier

LEASE_SECONDS = 45


class LeaseLost(RuntimeError):
    pass


async def claim(session, worker_id):
    query = (
        select(RunJob.run_id)
        .join(Run, Run.id == RunJob.run_id)
        .where(RunJob.status == "queued", Run.status == "queued")
        .order_by(Run.created_at, Run.id)
        .limit(1)
    )
    if session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=RunJob)
    candidate = await session.scalar(query)
    if candidate is None:
        return None
    token = await session.scalar(
        update(RunJob)
        .where(RunJob.run_id == candidate, RunJob.status == "queued")
        .values(
            status="running",
            worker_id=worker_id,
            fencing_token=RunJob.fencing_token + 1,
            lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS),
        )
        .returning(RunJob.fencing_token)
    )
    if token is None:
        await session.rollback()
        return None
    changed = await session.execute(
        update(Run)
        .where(Run.id == candidate, Run.status == "queued")
        .values(status="running")
    )
    if not changed.rowcount:
        await session.rollback()
        return None
    await append_event(session, candidate, "run_status", {"status": "running"})
    await session.commit()
    return candidate, token


async def fence(session, run_id, token):
    # UPDATE acquires a row lock on PostgreSQL and serializes cancellation,
    # checkpoint/event commits and new tool dispatch claims on SQLite too.
    changed = await session.execute(
        update(RunJob)
        .where(
            RunJob.run_id == run_id,
            RunJob.status == "running",
            RunJob.fencing_token == token,
            RunJob.lease_until > utcnow(),
            exists(select(Run.id).where(Run.id == run_id, Run.status == "running")),
        )
        .values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS))
    )
    if not changed.rowcount:
        raise LeaseLost("Worker ownership expired or run was cancelled")


async def recover_expired(session):
    expired = (
        await session.scalars(
            select(RunJob.run_id).where(
                RunJob.status == "running",
                RunJob.lease_until <= utcnow(),
            )
        )
    ).all()
    for run_id in expired:
        changed = await session.execute(
            update(RunJob)
            .where(
                RunJob.run_id == run_id,
                RunJob.status == "running",
                RunJob.lease_until <= utcnow(),
            )
            .values(
                status="interrupted",
                fencing_token=RunJob.fencing_token + 1,
                lease_until=None,
            )
        )
        if not changed.rowcount:
            continue
        uncertain = await session.scalar(
            select(ToolExecution.id)
            .where(
                ToolExecution.run_id == run_id,
                ToolExecution.is_write.is_(True),
                ToolExecution.status == "running",
            )
            .limit(1)
        )
        status = "unknown" if uncertain else "failed"
        await session.execute(
            update(ToolExecution)
            .where(ToolExecution.run_id == run_id, ToolExecution.status == "running")
            .values(status="unknown")
        )
        await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.not_in(TERMINAL))
            .values(
                status=status,
                active_key=Run.conversation_id if uncertain else None,
                error_code="WORKER_INTERRUPTED",
            )
        )
        await append_event(
            session,
            run_id,
            "run_status",
            {
                "status": status,
                "error_code": "WORKER_INTERRUPTED",
                "requires_reconciliation": bool(uncertain),
            },
        )
    await session.commit()


class DurableExecutor:
    def __init__(self, executor, sessions, run_id, token):
        self.executor, self.sessions, self.run_id, self.token = (
            executor,
            sessions,
            run_id,
            token,
        )

    async def execute_call(self, call):
        arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
        registry = getattr(self.executor, "registry", None)
        skill = registry.get(call.tool_name) if registry else None
        # Attribution for telemetry: the pinned package version, or "builtin".
        skill_ref = str(getattr(skill, "version_id", None) or "builtin")[:64]
        async with self.sessions() as session:
            await fence(session, self.run_id, self.token)
            existing = await session.scalar(
                select(ToolExecution).where(
                    ToolExecution.run_id == self.run_id,
                    ToolExecution.call_id == call.call_id,
                )
            )
            if existing:
                if (
                    existing.arguments != arguments
                    or existing.tool_name != call.tool_name
                ):
                    raise LeaseLost("Tool call ID was reused with different arguments")
                if existing.result:
                    return SkillResult.model_validate_json(existing.result)
                raise LeaseLost("Tool execution requires reconciliation")
            execution_id = step_identifier(self.run_id, "execution:" + call.call_id)
            session.add(
                ToolExecution(
                    id=execution_id,
                    run_id=self.run_id,
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=arguments,
                    status="running",
                    is_write=call.tool_name == "node_health_remediate"
                    and call.arguments.get("dry_run", True) is False,
                    started_at=utcnow(),
                    skill_ref=skill_ref,
                )
            )
            await session.commit()
        # The durable claim above is the authorization point. A cancellation
        # racing a dispatched connector cannot retract its remote side effect.
        began = time.monotonic()
        result = await self.executor.execute(call.tool_name, call.arguments)
        duration_ms = int((time.monotonic() - began) * 1000)
        async with self.sessions() as session:
            await fence(session, self.run_id, self.token)
            await session.execute(
                update(ToolExecution)
                .where(ToolExecution.id == execution_id)
                .values(
                    status="awaiting_approval"
                    if result.needs_approval
                    else "succeeded"
                    if result.success
                    else "failed",
                    result=result.model_dump_json(),
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
        return result
