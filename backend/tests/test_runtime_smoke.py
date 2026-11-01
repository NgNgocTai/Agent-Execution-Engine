from pathlib import Path
import asyncio
import sys
import tempfile
import unittest
from unittest.mock import patch
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings
from repositories import engine as database
from controllers import chat, runs, skills, operations
import main


class RuntimeSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_startup_accepts_a_durable_run_and_drains_on_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine("sqlite+aiosqlite:///" + Path(directory, "runtime.db").as_posix())
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            self.enterContext(patch.object(database, "engine", engine))
            self.enterContext(patch.object(database, "async_session", sessions))
            for module in (chat, runs, skills, operations): self.enterContext(patch.object(module, "async_session", sessions))
            self.enterContext(patch.object(chat, "engine", None))
            self.enterContext(patch.object(settings, "data_mode", "fixture"))
            self.enterContext(patch.object(settings, "worker_mode", "embedded"))
            self.enterContext(patch.object(settings, "deployment_mode", "development"))
            await main.startup()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                    self.assertEqual((await client.get("/ready")).status_code, 200)
                    self.assertEqual((await client.get("/api/config")).json()["providers"], ["mock"])
                    response = await client.post("/api/runs", json={"message": "Investigate a link alarm", "provider": "mock", "idempotency_key": "smoke"})
                    self.assertEqual(response.status_code, 202, response.text)
                    self.assertIn("x-request-id", response.headers)
                    run_id = response.json()["run_id"]
                    async with asyncio.timeout(15):
                        while True:
                            snapshot = (await client.get("/api/runs/" + run_id)).json()
                            if snapshot["status"] not in {"queued", "running"}: break
                            await asyncio.sleep(0.1)
                    self.assertEqual(snapshot["status"], "succeeded", snapshot)
                    replay = await client.get("/api/runs/" + run_id + "/events")
                    self.assertIn('"type": "final_answer"', replay.text)
            finally:
                await main.shutdown()
                await engine.dispose()
            self.assertTrue(main.app.state.worker_task.done())
