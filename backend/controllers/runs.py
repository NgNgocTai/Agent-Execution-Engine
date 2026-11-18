import asyncio
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from core.auth import require
from core.redaction import redact
from repositories.engine import async_session
from repositories.conversation_repo import ConversationRepository
from repositories.approval_repo import ApprovalRepository
from repositories.run_models import Run, RunEvent, RunJob, RunCheckpoint, RunStep
from services.run_service import (
    TERMINAL,
    create_run,
    get_owned,
    public_run,
    cancel_run,
    append_event,
)

router = APIRouter(prefix="/api", tags=["runs"])
_subscribers = asyncio.Semaphore(32)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=5000)
    provider: Literal["mock", "openai", "anthropic", "deepseek"] = "mock"
    conversation_id: str | None = Field(default=None, max_length=36)
    skill: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,99}$")
    idempotency_key: str = Field(
        default_factory=lambda: uuid4().hex, min_length=1, max_length=100
    )


@router.post("/runs", status_code=202)
async def start_run(req: CreateRunRequest):
    require("operator")
    if not req.message.strip():
        raise HTTPException(422, "Message must not be blank")
    from core.config import settings

    if settings.data_mode == "fixture" and req.provider != "mock":
        raise HTTPException(422, "Fixture mode only supports the mock provider")
    if settings.deployment_mode == "production" and req.provider == "mock":
        raise HTTPException(422, "Mock providers are disabled in production")
    async with async_session() as session:
        run = await create_run(session, **req.model_dump())
        return public_run(run)


@router.get("/conversations/{conversation_id}/runs")
async def list_runs(conversation_id: str, limit: int = Query(20, ge=1, le=100)):
    require("viewer", "operator", "approver")
    async with async_session() as session:
        if await ConversationRepository(session).get(conversation_id) is None:
            raise HTTPException(404, "Conversation not found")
        runs = (
            await session.scalars(
                select(Run)
                .where(Run.conversation_id == conversation_id)
                .order_by(Run.created_at.desc(), Run.id)
                .limit(limit)
            )
        ).all()
        return {"runs": [public_run(run) for run in runs]}


@router.get("/runs/options")
async def run_options():
    """Skills an operator may select. Declared before /runs/{run_id}."""
    require("viewer", "operator", "approver")
    from controllers import chat
    from repositories.skill_models import SkillActivation, SkillVersion
    from services.skill_catalog import BUILTIN_SKILL_NAMES

    skills = [{"name": "auto", "kind": "auto", "description": "The agent chooses from every approved skill"}]
    registry = getattr(getattr(chat, "engine", None), "skill_registry", None)
    for name in BUILTIN_SKILL_NAMES:
        skill = registry.get(name) if registry else None
        if skill is not None:
            skills.append({"name": name, "kind": "builtin", "description": skill.description[:240]})
    async with async_session() as session:
        rows = (
            await session.execute(
                select(SkillVersion)
                .join(SkillActivation, SkillActivation.version_id == SkillVersion.id)
                .where(SkillVersion.status == "approved")
                .order_by(SkillVersion.name)
            )
        ).scalars()
        for record in rows:
            manifest = json.loads(record.manifest)
            skills.append({"name": record.name, "kind": "custom", "version": record.version,
                           "description": str(manifest.get("description", ""))[:240]})
    return {"skills": skills}


@router.get("/runs/{run_id}")
async def snapshot(run_id: str):
    require("viewer", "operator", "approver")
    async with async_session() as session:
        run = await get_owned(session, run_id)
        events = (
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.sequence.desc())
                .limit(100)
            )
        ).all()
        messages = await ConversationRepository(session).get_messages(
            run.conversation_id
        )
        pending = await ApprovalRepository(session).list_pending(run.conversation_id)
        steps = (
            await session.scalars(
                select(RunStep)
                .where(RunStep.run_id == run_id)
                .order_by(RunStep.started_at, RunStep.id)
                .limit(1000)
            )
        ).all()
        return {
            **public_run(run),
            "messages": redact(messages),
            "pending_approvals": redact(pending),
            "steps": [
                {
                    "step_id": step.id,
                    "kind": step.kind,
                    "status": step.status,
                    "started_at": step.started_at.isoformat(),
                    "finished_at": step.finished_at.isoformat()
                    if step.finished_at
                    else None,
                    "usage": json.loads(step.usage) if step.usage else None,
                }
                for step in steps
            ],
            "events": [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "data": json.loads(event.payload),
                }
                for event in reversed(events)
            ],
        }


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
):
    require("viewer", "operator", "approver")
    if last_event_id is not None:
        try:
            after = int(last_event_id)
            if after < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "Invalid event cursor") from None
    async with async_session() as session:
        run = await get_owned(session, run_id)
        if after > run.sequence:
            raise HTTPException(
                409,
                {"code": "SNAPSHOT_REQUIRED", "message": "Cursor is ahead of the run"},
            )
        first = await session.scalar(
            select(RunEvent.sequence)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence)
            .limit(1)
        )
        if (first is not None and after < first - 1) or (
            first is None and after < run.sequence
        ):
            raise HTTPException(
                409,
                {
                    "code": "SNAPSHOT_REQUIRED",
                    "message": "Cursor is outside event retention",
                },
            )

    try:
        await asyncio.wait_for(_subscribers.acquire(), timeout=0.05)
    except TimeoutError:
        raise HTTPException(
            429, "Event subscriber limit reached; retry shortly"
        ) from None

    async def stream():
        try:
            cursor, idle = after, 0
            while not await request.is_disconnected():
                async with async_session() as session:
                    run = await get_owned(session, run_id)
                    batch = (
                        await session.scalars(
                            select(RunEvent)
                            .where(
                                RunEvent.run_id == run_id, RunEvent.sequence > cursor
                            )
                            .order_by(RunEvent.sequence)
                            .limit(32)
                        )
                    ).all()
                    status, end = run.status, run.sequence
                for event in batch:
                    cursor = event.sequence
                    data = json.dumps(
                        {
                            "event_id": f"{run_id}:{cursor}",
                            "run_id": run_id,
                            "sequence": cursor,
                            "type": event.event_type,
                            "data": json.loads(event.payload),
                        },
                        ensure_ascii=False,
                    )
                    yield f"id: {cursor}\ndata: {data}\n\n"
                if cursor >= end and (
                    status in TERMINAL or status == "waiting_approval"
                ):
                    return
                if not batch:
                    idle += 1
                    if idle >= 50:
                        yield ": heartbeat\n\n"
                        idle = 0
                    await asyncio.sleep(0.2)

        finally:
            _subscribers.release()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str):
    require("operator")
    async with async_session() as session:
        return public_run(await cancel_run(session, run_id))


class RunApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call_id: str = Field(min_length=1, max_length=100)
    approved: bool


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=2000)


@router.post("/runs/{run_id}/interventions", status_code=202)
async def intervene(run_id: str, req: InterventionRequest):
    require("operator")
    if not req.message.strip():
        raise HTTPException(422, "Intervention must not be blank")
    async with async_session() as session:
        await get_owned(session, run_id)
        locked = await session.execute(
            update(RunJob)
            .where(RunJob.run_id == run_id, RunJob.status == "waiting_approval")
            .values(status="waiting_approval")
        )
        if not locked.rowcount:
            raise HTTPException(409, "Intervention requires a safe approval checkpoint")
        checkpoint = await session.get(RunCheckpoint, run_id)
        if not checkpoint or checkpoint.version != 1:
            raise HTTPException(409, "Checkpoint is unavailable")
        state = json.loads(checkpoint.state)
        interventions = state.get("interventions", [])
        if len(interventions) >= 5:
            raise HTTPException(429, "Checkpoint intervention limit reached")
        state["interventions"] = [*interventions, redact(req.message)]
        checkpoint.state = json.dumps(state, ensure_ascii=False)
        await append_event(
            session,
            run_id,
            "intervention_accepted",
            {"message": "Operator intervention accepted for the next safe checkpoint"},
        )
        await session.commit()
        return {"status": "accepted", "applies_after": "current_tool_batch"}


@router.post("/runs/{run_id}/approve")
async def approve(run_id: str, req: RunApprovalRequest):
    require("approver")
    async with async_session() as session:
        run = await get_owned(session, run_id)
        checkpoint = await session.get(RunCheckpoint, run_id)
        state = (
            json.loads(checkpoint.state)
            if checkpoint and checkpoint.version == 1
            else None
        )
        if (
            run.status != "waiting_approval"
            or not state
            or not state["pending_calls"]
            or state["pending_calls"][0]["call_id"] != req.call_id
        ):
            raise HTTPException(409, "Run is not awaiting this approval")
        conversation_id = run.conversation_id
    from controllers.chat import approve_action, ApprovalDecisionRequest

    # Legacy handler owns the durable approval claim and records uncertain SSH.
    await approve_action(
        ApprovalDecisionRequest(
            conversation_id=conversation_id, call_id=req.call_id, approved=req.approved
        )
    )
    from services.approval_resume import queue_resolved_approval

    async with async_session() as session:
        await queue_resolved_approval(session, run_id)
        return public_run(await get_owned(session, run_id))
