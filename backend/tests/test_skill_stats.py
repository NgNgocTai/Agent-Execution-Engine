from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from core.auth import Principal, _principal
from core.clock import utcnow
from core.schemas import NormalizedToolCall
from repositories.models import Base
from repositories.run_models import Run, RunJob, ToolExecution
from services.job_service import DurableExecutor, claim
from services.run_service import create_run
from services.skill_stats import skill_stats
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor


class Tool(BaseSkill):
    parameters_schema = {"type": "object", "properties": {}}
    description = "tool"

    def __init__(self, name, status=SkillStatus.SUCCESS, version_id=None):
        self._name, self.status = name, status
        if version_id:
            self.version_id = version_id

    name = property(lambda self: self._name)

    async def execute(self, **kwargs):
        return SkillResult(status=self.status, data={"ok": True}, error=None if self.status == SkillStatus.SUCCESS else "bad")


class StatsFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = create_async_engine("sqlite+aiosqlite:///" + Path(directory.name, "stats.db").as_posix())
        self.addAsyncCleanup(self.db.dispose)
        async with self.db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db, expire_on_commit=False)
        token = _principal.set(Principal("op", frozenset({"operator"}), frozenset({"*"}), frozenset({"*"}), frozenset()))
        self.addCleanup(_principal.reset, token)

    async def add(self, n, tool, status, skill_ref="builtin", duration=None, age_days=0, timed=True):
        async with self.sessions() as session:
            session.add(ToolExecution(
                id=f"e{n}", run_id="r", call_id=f"c{n}", tool_name=tool, arguments="{}", status=status,
                started_at=utcnow() - timedelta(days=age_days) if timed else None,
                duration_ms=duration, skill_ref=skill_ref,
            ))
            await session.commit()

    async def stats(self, days=7):
        async with self.sessions() as session:
            return await skill_stats(session, days)


class AggregationTest(StatsFixture):
    async def test_counts_outcomes_latency_and_separates_reads_from_executions(self):
        await self.add(1, "custom_a", "succeeded", "v1", 100)
        await self.add(2, "custom_a", "succeeded", "v1", 300)
        await self.add(3, "custom_a", "failed", "v1", 50)
        await self.add(4, "custom_a", "unknown", "v1", None)
        await self.add(5, "custom_a", "succeeded", "v2", 10)
        await self.add(6, "load_skill", "succeeded", "builtin", 5)
        await self.add(7, "read_skill_file", "succeeded", "builtin", 7)
        result = await self.stats()
        rows = {(r["tool"], r["skill_ref"]): r for r in result["stats"]}
        v1 = rows[("custom_a", "v1")]
        self.assertEqual((v1["executions"], v1["succeeded"], v1["failed"], v1["unknown_or_running"]), (4, 2, 1, 1))
        self.assertEqual((v1["avg_ms"], v1["max_ms"], v1["timed_executions"]), (150.0, 300, 3))
        self.assertEqual(rows[("custom_a", "v2")]["executions"], 1)
        self.assertEqual({r["tool"]: r["kind"] for r in result["stats"]}, {"custom_a": "execute", "load_skill": "read", "read_skill_file": "read"})
        self.assertEqual(sum(r["executions"] for r in result["stats"] if r["kind"] == "execute"), 5)

    async def test_running_rows_are_never_counted_as_success_and_old_rows_are_excluded(self):
        await self.add(1, "t", "running", duration=None)
        await self.add(2, "t", "succeeded", duration=10, age_days=30)
        await self.add(3, "t", "succeeded", duration=10, timed=False)
        result = await self.stats(days=7)
        row = result["stats"][0]
        self.assertEqual((row["executions"], row["succeeded"], row["unknown_or_running"]), (1, 0, 1))
        self.assertEqual(result["untimed_legacy_rows"], 1)
        self.assertEqual((await self.stats(days=90))["stats"][0]["executions"], 2)

    async def test_window_is_bounded_and_empty_data_is_a_valid_answer(self):
        self.assertEqual((await self.stats(days=10**6))["days"], 90)
        self.assertEqual((await self.stats(days=0))["days"], 1)
        empty = await self.stats()
        self.assertEqual((empty["stats"], empty["untimed_legacy_rows"]), ([], 0))


class RecordingTest(StatsFixture):
    async def run_calls(self, registry, calls):
        async with self.sessions() as session:
            run = await create_run(session, message="hi", provider="mock", conversation_id=None, idempotency_key="k")
        async with self.sessions() as session:
            run_id, token = await claim(session, "w")
        executor = DurableExecutor(ToolExecutor(registry), self.sessions, run_id, token)
        for number, (name, ref) in enumerate(calls):
            await executor.execute_call(NormalizedToolCall(call_id=f"c{number}", tool_name=name, arguments={}))
        return run

    async def test_durable_executor_records_timing_and_the_pinned_skill_version(self):
        registry = SkillRegistry()
        registry.register(Tool("builtin_tool"))
        registry.register(Tool("custom_pkg", version_id="version-123"))
        registry.register(Tool("failing", status=SkillStatus.BUSINESS_ERROR, version_id="v9"))
        await self.run_calls(registry, [("builtin_tool", None), ("custom_pkg", None), ("failing", None)])
        async with self.sessions() as session:
            rows = {r.tool_name: r for r in (await session.scalars(select(ToolExecution))).all()}
        self.assertEqual({k: v.skill_ref for k, v in rows.items()}, {"builtin_tool": "builtin", "custom_pkg": "version-123", "failing": "v9"})
        self.assertTrue(all(r.started_at is not None and r.duration_ms is not None and r.duration_ms >= 0 for r in rows.values()))
        self.assertEqual(rows["failing"].status, "failed")
        stats = {(r["tool"], r["skill_ref"]): r for r in (await self.stats())["stats"]}
        self.assertEqual(stats[("failing", "v9")]["failed"], 1)
        self.assertEqual(stats[("custom_pkg", "version-123")]["succeeded"], 1)

    async def test_stats_and_runs_survive_a_run_without_executions(self):
        async with self.sessions() as session:
            await create_run(session, message="hi", provider="mock", conversation_id=None, idempotency_key="k2")
            self.assertEqual(await session.scalar(select(func.count()).select_from(Run)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(RunJob)), 1)
        self.assertEqual((await self.stats())["stats"], [])


class EndpointTest(StatsFixture):
    async def test_endpoint_needs_a_role_and_returns_the_aggregation(self):
        from controllers import operations

        await self.add(1, "t", "succeeded", duration=12)
        with patch.object(operations, "async_session", self.sessions):
            body = await operations.skill_statistics(days=7)
            self.assertEqual(body["stats"][0]["tool"], "t")
            token = _principal.set(Principal("g", frozenset({"guest"}), frozenset(), frozenset(), frozenset()))
            try:
                with self.assertRaises(HTTPException) as raised:
                    await operations.skill_statistics(days=7)
            finally:
                _principal.reset(token)
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
