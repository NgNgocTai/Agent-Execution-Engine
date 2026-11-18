import json
from datetime import timedelta
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, func, update
from core.auth import require
from core.clock import utcnow
from core.config import settings
from core.redaction import redact
from repositories.engine import async_session
from repositories.models import PendingApproval, RemediationGuard
from repositories.run_models import Run, RunJob, ToolExecution, RunCheckpoint, RunStep
from repositories.audit_repo import audit
from repositories.approval_repo import ApprovalRepository
from repositories.conversation_repo import ConversationRepository
from services.run_service import get_owned, append_event, public_run

router = APIRouter(prefix="/api/operations", tags=["operations"])


def require_dispatch_window_closed(approval):
    # A cancelled await cannot stop its SSH thread. Reconciliation must not
    # release the host guard while that thread can still dispatch a command.
    deadline = (
        approval.resolved_at + timedelta(seconds=settings.ssh_total_timeout_seconds + 1)
        if approval.status == "unknown" and approval.resolved_at
        else approval.expires_at
        + timedelta(
            seconds=settings.approval_action_timeout_seconds
            + settings.ssh_total_timeout_seconds
            + 1
        )
    )
    remaining = (deadline - utcnow()).total_seconds()
    if remaining > 0:
        import math

        raise HTTPException(
            409,
            f"Wait {math.ceil(remaining)} seconds for the possible dispatch window to close, then verify the remote outcome again",
        )


@router.get("/skill-stats")
async def skill_statistics(days: int = Query(7, ge=1, le=90)):
    require("viewer", "operator", "approver")
    from services.skill_stats import skill_stats

    async with async_session() as session:
        return await skill_stats(session, days)


@router.get("/connectors")
async def connectors(request: Request):
    require("viewer", "operator", "approver")
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        return {"sources": {}, "note": "No live data sources in this data mode"}
    return {"sources": await hub.probe()}


@router.get("/metrics")
async def metrics():
    require("admin")
    async with async_session() as session:
        statuses = (
            await session.execute(select(Run.status, func.count()).group_by(Run.status))
        ).all()
        executions = (
            await session.execute(
                select(ToolExecution.status, func.count()).group_by(
                    ToolExecution.status
                )
            )
        ).all()
        oldest = await session.scalar(
            select(func.min(Run.created_at)).where(Run.status == "queued")
        )
        expired = await session.scalar(
            select(func.count())
            .select_from(RunJob)
            .where(RunJob.status == "running", RunJob.lease_until <= utcnow())
        )
        approvals = await session.scalar(
            select(func.count())
            .select_from(PendingApproval)
            .where(PendingApproval.status == "pending")
        )
        sample = (
            await session.scalars(
                select(RunStep)
                .where(RunStep.finished_at.is_not(None))
                .order_by(RunStep.finished_at.desc())
                .limit(1000)
            )
        ).all()
        latencies, input_tokens, output_tokens, reported, model_steps = {}, 0, 0, 0, 0
        for step in sample:
            latencies.setdefault(step.kind, []).append(
                max(0, (step.finished_at - step.started_at).total_seconds() * 1000)
            )
            if step.kind == "llm":
                model_steps += 1
                usage = json.loads(step.usage) if step.usage else {}
                if all(
                    isinstance(usage.get(key), int) and usage[key] >= 0
                    for key in ("input", "output")
                ):
                    input_tokens += usage["input"]
                    output_tokens += usage["output"]
                    reported += 1
        import math

        latency_summary = {
            kind: {
                "count": len(values),
                "mean_ms": sum(values) / len(values),
                "p95_ms": sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)],
            }
            for kind, values in latencies.items()
        }
        return {
            "step_sample_window": "latest 1000 completed steps",
            "step_latency": latency_summary,
            "reported_input_tokens": input_tokens,
            "reported_output_tokens": output_tokens,
            "usage_reporting_complete": reported == model_steps,
            "reported_usage_steps": reported,
            "runs_by_status": dict(statuses),
            "executions_by_status": dict(executions),
            "pending_approvals": approvals,
            "expired_worker_leases": expired,
            "oldest_queued_age_seconds": max(0, (utcnow() - oldest).total_seconds())
            if oldest
            else 0,
        }


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["verified_succeeded", "verified_failed", "aborted_before_dispatch"]
    evidence: str = Field(min_length=20, max_length=4000)

    @field_validator("evidence")
    @classmethod
    def evidence_not_blank(cls, value):
        if len(value.strip()) < 20:
            raise ValueError("Provide meaningful reconciliation evidence")
        return value.strip()


@router.post("/runs/{run_id}/reconcile")
async def reconcile(run_id: str, req: Reconciliation):
    require("admin")
    async with async_session() as session:
        run = await get_owned(session, run_id)
        changed = await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "unknown")
            .values(status="failed", active_key=None, error_code="MANUALLY_RECONCILED")
        )
        if not changed.rowcount:
            raise HTTPException(409, "Only unknown runs can be manually reconciled")
        checkpoint = await session.get(RunCheckpoint, run_id)
        state = json.loads(checkpoint.state) if checkpoint else {}
        calls = [call["call_id"] for call in state.get("pending_calls", [])]
        if calls:
            approvals = (
                await session.scalars(
                    select(PendingApproval).where(
                        PendingApproval.conversation_id == run.conversation_id,
                        PendingApproval.call_id.in_(calls),
                        PendingApproval.status.in_(["executing", "unknown"]),
                    )
                )
            ).all()
            for approval in approvals:
                require_dispatch_window_closed(approval)
                approval.status = "reconciled"
                approval.resolved_at = utcnow()
                approval.error = "Outcome verified manually; no command was rerun"
                original = json.loads(approval.result) if approval.result else {}
                approval.result = json.dumps(
                    {**original, "manual_reconciliation": redact(req.model_dump())}
                )
                await session.execute(
                    update(RemediationGuard)
                    .where(RemediationGuard.approval_id == approval.id)
                    .values(
                        available_after=utcnow()
                        + timedelta(seconds=settings.remediation_cooldown_seconds)
                    )
                )
        await session.execute(
            update(ToolExecution)
            .where(
                ToolExecution.run_id == run_id,
                ToolExecution.status.in_(["running", "unknown"]),
            )
            .values(status="reconciled")
        )
        await session.execute(
            update(RunJob)
            .where(RunJob.run_id == run_id)
            .values(
                status="failed",
                lease_until=None,
                fencing_token=RunJob.fencing_token + 1,
            )
        )
        await audit(session, "run.reconciled", run_id, req.model_dump())
        await append_event(
            session,
            run_id,
            "run_status",
            {
                "status": "failed",
                "error_code": "MANUALLY_RECONCILED",
                "outcome": req.outcome,
                "evidence": redact(req.evidence),
                "message": "Manually reconciled. Start a new run if further work is needed.",
            },
        )
        await session.commit()
        await session.refresh(run)
        return public_run(run)


@router.get("/approvals/{conversation_id}/{call_id}")
async def inspect_approval(conversation_id: str, call_id: str):
    require("admin")
    async with async_session() as session:
        if await ConversationRepository(session).get(conversation_id) is None:
            raise HTTPException(404, "Conversation not found")
        approval = await ApprovalRepository(session).get(conversation_id, call_id)
        if approval is None:
            raise HTTPException(404, "Approval not found")
        return redact(
            {
                **ApprovalRepository.to_dict(approval),
                "result": json.loads(approval.result) if approval.result else None,
            }
        )


@router.post("/approvals/{conversation_id}/{call_id}/reconcile")
async def reconcile_approval(conversation_id: str, call_id: str, req: Reconciliation):
    require("admin")
    async with async_session() as session:
        if await ConversationRepository(session).get(conversation_id) is None:
            raise HTTPException(404, "Conversation not found")
        approval = await ApprovalRepository(session).get(conversation_id, call_id)
        if not approval or approval.status not in {"executing", "unknown"}:
            raise HTTPException(409, "Approval does not require reconciliation")
        require_dispatch_window_closed(approval)
        original = json.loads(approval.result) if approval.result else {}
        observed = json.dumps(
            {**original, "manual_reconciliation": redact(req.model_dump())}
        )
        changed = await session.execute(
            update(PendingApproval)
            .where(
                PendingApproval.id == approval.id,
                PendingApproval.status.in_(["executing", "unknown"]),
            )
            .values(
                status="reconciled",
                resolved_at=utcnow(),
                result=observed,
                error="Manually reconciled; no command rerun",
            )
        )
        if not changed.rowcount:
            raise HTTPException(409, "Approval changed concurrently")
        await session.execute(
            update(RemediationGuard)
            .where(RemediationGuard.approval_id == approval.id)
            .values(
                available_after=utcnow()
                + timedelta(seconds=settings.remediation_cooldown_seconds)
            )
        )
        await audit(
            session,
            "approval.reconciled",
            f"{conversation_id}:{call_id}",
            req.model_dump(),
        )
        await session.commit()
        return {
            "status": "reconciled",
            "command_rerun": False,
            "outcome": req.outcome,
            "message": "Observation recorded; no command was rerun",
        }
