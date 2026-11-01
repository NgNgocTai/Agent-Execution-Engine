"""Recover the commit gap between approval resolution and workflow resumption."""

import json
from sqlalchemy import update
from repositories.run_models import Run, RunJob, RunCheckpoint
from repositories.approval_repo import ApprovalRepository
from services.run_service import append_event
from services.workflow.state import RunState
from core.clock import utcnow


async def queue_resolved_approval(session, run_id):
    locked = await session.execute(
        update(RunJob)
        .where(RunJob.run_id == run_id, RunJob.status == "waiting_approval")
        .values(status="waiting_approval")
    )
    if not locked.rowcount:
        await session.rollback()
        return False
    run = await session.get(Run, run_id, populate_existing=True)
    checkpoint = await session.get(RunCheckpoint, run_id, populate_existing=True)
    if (
        not run
        or run.status != "waiting_approval"
        or not checkpoint
    ):
        await session.rollback()
        return False
    try:
        if checkpoint.version != 1:
            raise ValueError("Unsupported checkpoint version")
        state = RunState.model_validate_json(checkpoint.state).model_dump(mode="json")
    except ValueError:
        run.status, run.error_code = "unknown", "CHECKPOINT_INCOMPATIBLE"
        await session.execute(
            update(RunJob).where(RunJob.run_id == run_id).values(status="unknown")
        )
        await append_event(
            session,
            run_id,
            "run_status",
            {"status": "unknown", "error_code": "CHECKPOINT_INCOMPATIBLE"},
        )
        await session.commit()
        return True
    pending = state.get("pending_calls", [])
    if not pending:
        await session.rollback()
        return False
    call = pending[0]
    approval = await ApprovalRepository(session).get(
        run.conversation_id, call["call_id"]
    )
    if approval and approval.status == "pending" and approval.expires_at <= utcnow():
        expired_result = {
            "approval_status": "expired",
            "executed": False,
            "command_succeeded": False,
            "stderr": "Approval expired before execution",
        }
        if not await ApprovalRepository(session).resolve(
            approval,
            "expired",
            result=expired_result,
            error=expired_result["stderr"],
            commit=False,
        ):
            await session.rollback()
            return False
    if not approval or approval.status not in {
        "approved",
        "rejected",
        "unknown",
        "expired",
    }:
        await session.rollback()
        return False
    result = (
        json.loads(approval.result)
        if approval.result
        else {
            "approval_status": "expired",
            "executed": False,
            "command_succeeded": False,
            "stderr": "Approval expired",
        }
        if approval.status == "expired"
        else None
    )
    if approval.status == "unknown" or not result:
        status = "unknown"
    else:
        state["messages"].append(
            {
                "role": "tool",
                "tool_call_id": call["call_id"],
                "tool_name": approval.tool_name,
                "content": json.dumps(result, ensure_ascii=False),
                "is_error": not bool(result.get("command_succeeded")),
            }
        )
        state["pending_calls"] = pending[1:]
        checkpoint.state = json.dumps(state, ensure_ascii=False)
        status = "queued"
        await append_event(
            session,
            run_id,
            "tool_call_result",
            {
                "call_id": call["call_id"],
                "tool_name": approval.tool_name,
                "status": "success"
                if result.get("command_succeeded")
                else "business_error"
                if approval.status in {"rejected", "expired"}
                else "system_error",
                "result": result,
                "error": result.get("stderr"),
            },
        )
    run.status = status
    await session.execute(
        update(RunJob).where(RunJob.run_id == run_id).values(status=status)
    )
    await append_event(session, run_id, "run_status", {"status": status})
    await session.commit()
    return True
