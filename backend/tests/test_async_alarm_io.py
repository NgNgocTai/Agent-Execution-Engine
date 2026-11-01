import asyncio
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill


class AlarmIOTest(unittest.IsolatedAsyncioTestCase):
    async def test_alarm_query_does_not_block_event_loop(self):
        started, release = threading.Event(), threading.Event()

        class Devices:
            async def get_device_with_details(self, **kwargs):
                return {"device_id": "d", "name": "d", "device_type": "router", "ip_address": "127.0.0.1"}

        class Alarms:
            def get_active_alarms_by_device(self, *args, **kwargs):
                started.set()
                release.wait(2)
                return []

        task = asyncio.create_task(DeviceInventoryLookupSkill(Alarms(), Devices()).execute(device_id="d"))
        try:
            await asyncio.wait_for(asyncio.to_thread(started.wait, 1), 1.5)
            self.assertTrue(started.is_set())
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertTrue((await task).success)
