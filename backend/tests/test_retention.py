from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.clock import utcnow
from repositories.models import Base, Message, AuditEntry
from repositories.run_models import Run, RunEvent, RunCheckpoint
from services.run_service import create_run
from scripts.retention import prune


class RetentionTest(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_is_readonly_and_apply_preserves_history_audit_and_unknown_work(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine("sqlite+aiosqlite:///" + Path(directory, "retention.db").as_posix())
            try:
                async with engine.begin() as connection: await connection.run_sync(Base.metadata.create_all)
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    runs = [await create_run(session, message="hello", provider="mock", conversation_id=None, idempotency_key=str(index)) for index in range(3)]
                    old = utcnow() - timedelta(days=40)
                    for run, status in zip(runs, ("succeeded", "unknown", "running")):
                        await session.execute(update(Run).where(Run.id == run.id).values(status=status, active_key=None if status == "succeeded" else run.conversation_id, updated_at=old))
                        await session.execute(update(RunEvent).where(RunEvent.run_id == run.id).values(created_at=old))
                        session.add(RunCheckpoint(run_id=run.id, version=1, state="{}"))
                    session.add(Message(conversation_id=runs[0].conversation_id, role="assistant", content="Canonical final answer"))
                    await session.commit()
                    preview = await prune(session)
                    self.assertEqual((preview["events"], preview["checkpoints"]), (1, 1))
                    self.assertEqual(await session.scalar(select(func.count()).select_from(RunEvent)), 3)
                    await prune(session, apply=True)
                    self.assertEqual(await session.scalar(select(func.count()).select_from(RunEvent)), 2)
                    self.assertEqual(await session.scalar(select(func.count()).select_from(RunCheckpoint)), 2)
                    self.assertEqual(await session.scalar(select(func.count()).select_from(Run)), 3)
                    self.assertEqual(await session.scalar(select(func.count()).select_from(Message)), 4)
                    self.assertEqual(await session.scalar(select(func.count()).select_from(AuditEntry)), 4)
            finally: await engine.dispose()
