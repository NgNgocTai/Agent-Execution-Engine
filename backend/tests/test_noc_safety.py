import asyncio
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.clock import utcnow
from connectors.ssh import SSHResult
from repositories.models import Base
from repositories.approval_repo import ApprovalRepository
from repositories.conversation_repo import ConversationRepository
from services.metrics_script import METRICS_CODE
from services.skills.node_health_remediate import NodeHealthRemediateSkill
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill


class MetricsTest(unittest.TestCase):
    def test_cpu_uses_interval_and_memory_is_independent_of_line_order(self):
        samples = iter(["cpu 1 0 1 998 0 0 0 0 0 0\n", "cpu 51 0 51 1098 0 0 0 0 0 0\n"])
        def opened(path):
            return StringIO(next(samples) if path == "/proc/stat" else "MemFree: 10 kB\nBuffers: 99 kB\nMemAvailable: 400 kB\nMemTotal: 1000 kB\n" if path == "/proc/meminfo" else "1.25 0.0 0.0\n")
        output = StringIO()
        with patch("builtins.open", side_effect=opened), patch("time.sleep"), patch("os.statvfs", create=True, return_value=SimpleNamespace(f_bavail=25, f_blocks=100)), redirect_stdout(output):
            exec(compile(METRICS_CODE, "metrics-fixture", "exec"), {})
        self.assertEqual(json.loads(output.getvalue()), {"cpu_pct": 50.0, "mem_pct": 60.0, "load1": 1.25, "disk_pct": 75.0})

    def test_command_success_does_not_imply_service_recovery(self):
        skill = NodeHealthRemediateSkill()
        with patch.object(skill, "_ssh_exec", side_effect=[SSHResult(0, "done", ""), *[SSHResult(0, "false", "")] * 3]), patch("time.sleep"):
            result = skill.execute_approved_command("node-a", "docker restart api")
        self.assertTrue(result["command_succeeded"])
        self.assertEqual(result["verification"]["status"], "not_recovered")


class RemediationGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_different_conversations_cannot_remediate_one_host_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine("sqlite+aiosqlite:///" + Path(directory, "guard.db").as_posix())
            try:
                async with engine.begin() as connection: await connection.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                conversations = []
                async with sessions() as session:
                    for index in range(2):
                        conversation = await ConversationRepository(session).create("mock")
                        conversations.append(conversation.id)
                        await ApprovalRepository(session).create_pending(conversation.id, str(index), "node_health_remediate", {},
                            {"host": "same-node", "action": "restart_service", "command": "docker restart api"}, utcnow() + timedelta(minutes=5))
                async def claimed(index):
                    async with sessions() as session:
                        repo = ApprovalRepository(session)
                        approval = await repo.get(conversations[index], str(index))
                        return await repo.claim_pending(approval, approved=True)
                results = await asyncio.gather(claimed(0), claimed(1))
                self.assertEqual(sum(results), 1)
                winner = results.index(True)
                async with sessions() as session:
                    repo = ApprovalRepository(session)
                    approval = await repo.get(conversations[winner], str(winner))
                    await repo.resolve(approval, "unknown", result={"executed": None})
                self.assertFalse(await claimed(1 - winner), "Unknown commands must hold the per-host guard")
            finally: await engine.dispose()

    async def test_ambiguous_inventory_does_not_choose_first_device(self):
        class Devices:
            async def find_device_candidates(self, **kwargs): return [{"device_id": "one"}, {"device_id": "two"}]
        alarms = Mock()
        result = await DeviceInventoryLookupSkill(alarms, Devices()).execute(ip_address="10.0.0.1")
        self.assertFalse(result.success)
        self.assertEqual(len(result.data["candidates"]), 2)
        alarms.get_active_alarms_by_device.assert_not_called()
