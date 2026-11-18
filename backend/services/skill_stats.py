"""Execution statistics per tool and skill version, from durable execution rows.

Reads of instructions (load_skill/read_skill_file) are reported separately from
real executions so a skill that is often read but rarely run is visible. Labels
are tool name and pinned version only: bounded cardinality, no user data.
"""

from datetime import timedelta

from sqlalchemy import case, func, select

from core.clock import utcnow
from repositories.run_models import ToolExecution
from services.skill_guides import GUIDE_TOOLS

MAX_DAYS = 90


async def skill_stats(session, days=7):
    days = max(1, min(int(days), MAX_DAYS))
    since = utcnow() - timedelta(days=days)

    def count_if(*statuses):
        return func.sum(case((ToolExecution.status.in_(statuses), 1), else_=0))

    rows = (
        await session.execute(
            select(
                ToolExecution.tool_name,
                ToolExecution.skill_ref,
                func.count(),
                count_if("succeeded"),
                count_if("failed"),
                count_if("unknown", "running"),
                func.avg(ToolExecution.duration_ms),
                func.max(ToolExecution.duration_ms),
                func.count(ToolExecution.duration_ms),
            )
            .where(ToolExecution.started_at >= since)
            .group_by(ToolExecution.tool_name, ToolExecution.skill_ref)
            .order_by(ToolExecution.tool_name, ToolExecution.skill_ref)
        )
    ).all()
    legacy = await session.scalar(
        select(func.count()).select_from(ToolExecution).where(ToolExecution.started_at.is_(None))
    )
    stats = [
        {
            "tool": tool,
            "skill_ref": ref,
            "kind": "read" if tool in GUIDE_TOOLS else "execute",
            "executions": total,
            "succeeded": int(ok or 0),
            "failed": int(failed or 0),
            # Unknown outcomes (and still-running rows) are never counted as success.
            "unknown_or_running": int(unknown or 0),
            "avg_ms": round(float(avg), 1) if avg is not None else None,
            "max_ms": maximum,
            "timed_executions": timed,
        }
        for tool, ref, total, ok, failed, unknown, avg, maximum, timed in rows
    ]
    return {"days": days, "since": since.isoformat(), "stats": stats, "untimed_legacy_rows": legacy}
