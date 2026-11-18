"""Delivery lifecycle of operator interventions.

An intervention is accepted at a safe checkpoint, shown to the model after the
current tool batch, and only then is it "delivered". If the run ends first it is
"undelivered": the API never implies an instruction was applied because it was
accepted. A pending row of a finished run reads as undelivered even if no code
path settled it (crash, lease loss), so the state can never be stuck pending.
"""

from uuid import uuid4

from sqlalchemy import select, update

from core.clock import utcnow
from repositories.run_models import RunIntervention

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "unknown"}


async def record(session, run_id, message):
    item = RunIntervention(id=uuid4().hex, run_id=run_id, message=message, status="pending")
    session.add(item)
    await session.flush()
    return item.id


async def mark_delivered(session, run_id):
    result = await session.execute(
        update(RunIntervention)
        .where(RunIntervention.run_id == run_id, RunIntervention.status == "pending")
        .values(status="delivered", delivered_at=utcnow())
    )
    return result.rowcount


async def settle(session, run_id):
    """The run ended: whatever was never shown to the model was not applied."""
    result = await session.execute(
        update(RunIntervention)
        .where(RunIntervention.run_id == run_id, RunIntervention.status == "pending")
        .values(status="undelivered")
    )
    return result.rowcount


def effective_status(row, run_status):
    return "undelivered" if row.status == "pending" and run_status in TERMINAL else row.status


async def listing(session, run_id, run_status):
    rows = (
        await session.scalars(
            select(RunIntervention)
            .where(RunIntervention.run_id == run_id)
            .order_by(RunIntervention.created_at, RunIntervention.id)
        )
    ).all()
    return [
        {
            "id": row.id,
            "message": row.message,
            "status": effective_status(row, run_status),
            "created_at": row.created_at.isoformat(),
            "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        }
        for row in rows
    ]
