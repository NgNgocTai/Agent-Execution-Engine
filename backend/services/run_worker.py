import asyncio
from contextlib import suppress
import json
from uuid import uuid4
from sqlalchemy import select, update

from core.auth import LOCAL, Principal, _principal, require
from core.config import settings
from core.schemas import AgentEventType
from repositories.approval_repo import ApprovalRepository
from repositories.conversation_repo import ConversationRepository
from repositories.run_models import Run, RunJob, RunCheckpoint, ToolExecution
from repositories.audit_repo import audit
from services.agent_service import AgentEngine
from services.job_service import (
    DurableExecutor,
    LeaseLost,
    claim,
    fence,
    recover_expired,
)
from services.run_service import append_event
from services.skill_catalog import registry_for_run
from services.tool_executor import ToolExecutor
from core.request_context import run_id as current_run_id
from services.workflow.state import RunState, CheckpointIncompatible
from services.providers.lifecycle import close_provider


def actor_for_subject(subject):
    if settings.auth_mode == "local" and subject == "local":
        return LOCAL
    for entry in json.loads(settings.auth_principals_json):
        if entry["subject"] == subject:
            return Principal(
                subject,
                frozenset(entry["roles"]),
                frozenset(host.lower() for host in entry.get("hosts", [])),
                frozenset(service.lower() for service in entry.get("services", [])),
            )
    raise LeaseLost("Run actor was revoked")


class RunWorker:
    def __init__(self, engine, sessions, provider_factory):
        self.engine, self.sessions, self.provider_factory = (
            engine,
            sessions,
            provider_factory,
        )
        self.worker_id = uuid4().hex
        self.stop = asyncio.Event()
        self.approval_cursor = None

    async def heartbeat(self, run_id, token):
        while True:
            await asyncio.sleep(10)
            async with self.sessions() as session:
                await fence(session, run_id, token)
                await session.commit()

    async def execute(self, run_id, token):
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            actor = actor_for_subject(run.actor)
            context = _principal.set(actor)
            correlation = current_run_id.set(run_id)
            provider = None
            try:
                require("operator")
                if (
                    await ConversationRepository(session).get(run.conversation_id)
                    is None
                ):
                    raise LeaseLost(
                        "Run actor no longer has access to this conversation"
                    )
                history = await ConversationRepository(session).get_messages(
                    run.conversation_id
                )
                # The user input was already committed when accepting the run.
                checkpoint_record = await session.get(RunCheckpoint, run_id)
                if checkpoint_record and checkpoint_record.version != 1:
                    raise CheckpointIncompatible("Unsupported checkpoint version")
                try:
                    checkpoint = (
                        RunState.model_validate_json(
                            checkpoint_record.state
                        ).model_dump(mode="json")
                        if checkpoint_record
                        else None
                    )
                except ValueError as exc:
                    raise CheckpointIncompatible(
                        "Checkpoint state is incompatible"
                    ) from exc
                registry = await registry_for_run(
                    self.engine.skill_registry, session, self.sessions, run
                )
                await session.commit()
                engine = AgentEngine(
                    DurableExecutor(
                        ToolExecutor(registry), self.sessions, run_id, token
                    ),
                    registry,
                    self.engine.tracer,
                )

                async def save_checkpoint(state):
                    state = RunState.model_validate(state).model_dump(mode="json")
                    await fence(session, run_id, token)
                    record = await session.get(RunCheckpoint, run_id)
                    if record is None:
                        session.add(
                            RunCheckpoint(
                                run_id=run_id,
                                version=1,
                                state=json.dumps(state, ensure_ascii=False),
                            )
                        )
                    else:
                        record.state = json.dumps(state, ensure_ascii=False)

                provider = self.provider_factory(run.provider)
                async for event in engine.run(
                    run.conversation_id,
                    run.user_message,
                    provider,
                    history,
                    ApprovalRepository(session, commit_on_create=False),
                    resume=True,
                    checkpoint=checkpoint,
                    save_checkpoint=save_checkpoint,
                ):
                    await fence(session, run_id, token)
                    terminal = event.type in {
                        AgentEventType.FINAL_ANSWER,
                        AgentEventType.ERROR,
                        AgentEventType.APPROVAL_REQUIRED,
                    }
                    if terminal and event.data.get("new_messages"):
                        # Use the same transaction for messages, checkpoint,
                        # approval snapshot, terminal status and final event.
                        from controllers.chat import _message_value

                        records = []
                        for message in event.data["new_messages"]:
                            calls = _message_value(message, "tool_calls")
                            records.append(
                                {
                                    "role": _message_value(message, "role"),
                                    "content": _message_value(message, "content"),
                                    "thinking_content": _message_value(
                                        message, "thinking_content"
                                    ),
                                    "tool_calls": [
                                        call.model_dump()
                                        if hasattr(call, "model_dump")
                                        else call
                                        for call in calls
                                    ]
                                    if calls
                                    else None,
                                    "tool_call_id": _message_value(
                                        message, "tool_call_id"
                                    ),
                                    "name": _message_value(message, "tool_name"),
                                    "is_error": _message_value(
                                        message, "is_error", False
                                    ),
                                }
                            )
                        await ConversationRepository(session).add_messages(
                            run.conversation_id, records, commit=False
                        )
                    if terminal:
                        status = {
                            AgentEventType.FINAL_ANSWER: "succeeded",
                            AgentEventType.ERROR: "failed",
                            AgentEventType.APPROVAL_REQUIRED: "waiting_approval",
                        }[event.type]
                        await session.execute(
                            update(Run)
                            .where(Run.id == run_id, Run.status == "running")
                            .values(
                                status=status,
                                active_key=run.conversation_id
                                if status == "waiting_approval"
                                else None,
                                error_code=event.data.get("code"),
                            )
                        )
                        await session.execute(
                            update(RunJob)
                            .where(RunJob.run_id == run_id)
                            .values(status=status, lease_until=None)
                        )
                        await audit(session, "run." + status, run_id)
                    if (
                        event.type == AgentEventType.TOOL_CALL_RESULT
                        and isinstance(event.data.get("result"), dict)
                        and event.data["result"].get("error_code") == "POLICY_DENIED"
                    ):
                        await audit(
                            session,
                            "policy.denied",
                            run_id,
                            {"tool": event.data.get("tool_name")},
                        )
                    # Full reasoning remains internal; persist useful progress.
                    if event.type == AgentEventType.THINKING:
                        applied = (
                            event.data.get("content") == "Operator intervention applied"
                        )
                        await append_event(
                            session,
                            run_id,
                            "intervention_applied" if applied else "progress",
                            {
                                "message": "Operator intervention applied"
                                if applied
                                else "Analyzing the request",
                                "iteration": event.iteration,
                            },
                        )
                    else:
                        await append_event(
                            session, run_id, event.type.value, event.data
                        )
                    if terminal:
                        await append_event(
                            session, run_id, "run_status", {"status": status}
                        )
                    await session.commit()
            finally:
                if provider is not None:
                    await close_provider(provider)
                current_run_id.reset(correlation)
                _principal.reset(context)

    async def process(self, run_id, token):
        execution = asyncio.create_task(self.execute(run_id, token))
        heartbeat = asyncio.create_task(self.heartbeat(run_id, token))
        try:
            async with asyncio.timeout(settings.run_timeout_seconds):
                done, _ = await asyncio.wait(
                    [execution, heartbeat], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    await task
        except (Exception, asyncio.CancelledError) as exc:
            execution.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await execution
            async with self.sessions() as session:
                try:
                    await fence(session, run_id, token)
                    uncertain = await session.scalar(
                        select(ToolExecution.id)
                        .where(
                            ToolExecution.run_id == run_id,
                            ToolExecution.is_write.is_(True),
                            ToolExecution.status == "running",
                        )
                        .limit(1)
                    )
                    status = (
                        "unknown"
                        if uncertain
                        else "timed_out"
                        if isinstance(exc, TimeoutError)
                        else "failed"
                    )
                    error_code = (
                        "CHECKPOINT_INCOMPATIBLE"
                        if isinstance(exc, CheckpointIncompatible)
                        else "RUN_INTERRUPTED"
                    )
                    await session.execute(
                        update(ToolExecution)
                        .where(
                            ToolExecution.run_id == run_id,
                            ToolExecution.status == "running",
                        )
                        .values(status="unknown")
                    )
                    await session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status=status,
                            active_key=Run.conversation_id if uncertain else None,
                            error_code=error_code,
                        )
                    )
                    await session.execute(
                        update(RunJob)
                        .where(RunJob.run_id == run_id)
                        .values(status=status, lease_until=None)
                    )
                    await append_event(
                        session,
                        run_id,
                        "run_status",
                        {"status": status, "error_code": error_code},
                    )
                    await session.commit()
                except LeaseLost:
                    await session.rollback()
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            heartbeat.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await heartbeat

    async def run_forever(self):
        while not self.stop.is_set():
            async with self.sessions() as session:
                await recover_expired(session)
                query = (
                    select(Run.id)
                    .where(Run.status == "waiting_approval")
                    .order_by(Run.id)
                    .limit(100)
                )
                if self.approval_cursor:
                    query = query.where(Run.id > self.approval_cursor)
                waiting = (await session.scalars(query)).all()
                self.approval_cursor = waiting[-1] if waiting else None
                from services.approval_resume import queue_resolved_approval

                for run_id in waiting:
                    await queue_resolved_approval(session, run_id)
                job = await claim(session, self.worker_id)
            if job:
                await self.process(*job)
            else:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
