"""Dry-run by default; preserve audit, messages, runs and uncertain executions."""
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, delete, func
from core.clock import utcnow
from repositories.engine import async_session
from repositories.models import AuditEntry
from repositories.run_models import Run, RunEvent, RunCheckpoint


async def prune(session, days=30, apply=False):
    if days < 7: raise ValueError("Event retention cannot be shorter than seven days")
    cutoff = utcnow() - timedelta(days=days)
    retired = select(Run.id).where(Run.status.in_(["succeeded", "failed", "cancelled", "timed_out"]), Run.active_key.is_(None), Run.updated_at < cutoff)
    event_filter = (RunEvent.run_id.in_(retired), RunEvent.created_at < cutoff)
    events = await session.scalar(select(func.count()).select_from(RunEvent).where(*event_filter))
    checkpoints = await session.scalar(select(func.count()).select_from(RunCheckpoint).where(RunCheckpoint.run_id.in_(retired)))
    report = {"apply": apply, "cutoff_utc": cutoff.isoformat(), "events": events, "checkpoints": checkpoints}
    if apply:
        await session.execute(delete(RunEvent).where(*event_filter))
        await session.execute(delete(RunCheckpoint).where(RunCheckpoint.run_id.in_(retired)))
        session.add(AuditEntry(actor="retention_job", action="retention.applied", resource="retired_run_events", details=json.dumps(report)))
        await session.commit()
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed retention policy instead of a dry run")
    args = parser.parse_args()
    async with async_session() as session: print(json.dumps(await prune(session, args.days, args.apply)))


if __name__ == "__main__": asyncio.run(main())
