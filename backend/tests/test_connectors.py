import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.auth import Principal, _principal
from core.config import settings
from services import connectors
from services.connectors import (
    ConnectorHub,
    ConnectorUnavailable,
    HEALTHY,
    NOT_CONFIGURED,
    UNAVAILABLE,
    UNKNOWN,
)
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor

SECRET = "hunter2-db-password"


class FakeAlarms:
    instances = []

    def __init__(self):
        self.closed = False
        FakeAlarms.instances.append(self)

    def get_alarms(self, code, window=10, limit=50):
        return [{"alarm_id": 1, "error_code": code}], 1

    def get_active_alarms_by_device(self, device_id, limit=20):
        return [{"alarm_id": 7}]

    def _query(self, sql):
        return [[1]]

    def close(self):
        self.closed = True


class FakeDevices:
    def __init__(self):
        self.closed, self.connected = False, False
        self._pool = NS(fetchval=self._fetchval)

    async def _fetchval(self, sql):
        return 1

    async def connect(self):
        self.connected = True

    async def find_device_candidates(self, **kwargs):
        return [{"device_id": "d1", "name": "router-1", "device_type": "router", "ip_address": "10.0.0.1", "vendor_name": "V", "station_name": "S"}]

    async def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def down(*_):
    raise OSError(f"connect to clickhouse://user:{SECRET}@10.1.1.1 refused")


def hub(alarm_factory=FakeAlarms, device_factory=FakeDevices, clock=None):
    with patch.object(settings, "clickhouse_host", "ch"), patch.object(
        settings, "postgres_host", "pg"
    ):
        created = ConnectorHub(alarm_factory, device_factory)
    if clock:
        created.alarms.clock = created.devices.clock = clock
    return created


class IsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_starts_without_touching_any_source(self):
        from services.runtime import create_runtime

        with patch("repositories.clickhouse_repo.AlarmRepository", side_effect=OSError) as ch, patch(
            "repositories.postgres_repo.DeviceRepository", side_effect=OSError
        ) as pg, patch.object(settings, "data_mode", "live"), patch.object(
            settings, "clickhouse_host", "unreachable"
        ), patch.object(settings, "postgres_host", "unreachable"):
            engine, resources = await create_runtime()
        ch.assert_not_called()
        pg.assert_not_called()
        self.assertEqual(
            set(engine.skill_registry.list_names()),
            {
                "noc_alarm_enrichment",
                "alarm_statistics",
                "device_inventory_lookup",
                "site_inventory",
                "site_alarm_overview",
                "schema_catalog",
            },
        )
        self.assertTrue(any(isinstance(r, ConnectorHub) for r in resources))

    async def test_alarm_outage_keeps_inventory_usable_and_never_reports_zero_alarms(self):
        shared = hub(alarm_factory=down)
        skill = DeviceInventoryLookupSkill(shared.alarms, shared.devices)
        result = await skill.execute(device_name="router-1")
        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertEqual(result.data["device"]["device_id"], "d1")
        self.assertEqual(result.data["alarm_source"], "unavailable")
        self.assertIsNone(result.data["active_alarms"])
        self.assertIsNone(result.data["active_alarm_count"])
        self.assertNotIn(SECRET, json.dumps(result.data))
        self.assertEqual(shared.status()["alarms"]["state"], UNAVAILABLE)
        self.assertEqual(shared.status()["inventory"]["state"], HEALTHY)

    async def test_inventory_outage_does_not_block_alarm_queries(self):
        class Broken(FakeDevices):
            async def connect(self):
                raise ConnectionError(SECRET)

        shared = hub(device_factory=Broken)
        alarms, total = await asyncio.to_thread(shared.alarms.get_alarms, "LINK_DOWN")
        self.assertEqual(total, 1)
        with self.assertRaises(ConnectorUnavailable):
            await shared.devices.find_device_candidates(device_name="x")
        self.assertEqual(shared.status()["alarms"]["state"], HEALTHY)
        self.assertEqual(shared.status()["inventory"]["state"], UNAVAILABLE)

    async def test_tool_executor_reports_a_failed_source_as_a_distinct_error(self):
        shared = hub(alarm_factory=down)

        class NeedsAlarms(BaseSkill):
            name = "needs_alarms"
            description = "x"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                rows, _ = await asyncio.to_thread(shared.alarms.get_alarms, "X")
                return SkillResult(status=SkillStatus.SUCCESS, data=rows)

        registry = SkillRegistry()
        registry.register(NeedsAlarms())
        token = _principal.set(Principal("op", frozenset({"operator"}), frozenset({"*"}), frozenset({"*"})))
        self.addCleanup(_principal.reset, token)
        result = await ToolExecutor(registry).execute("needs_alarms", {})
        self.assertEqual(result.status, SkillStatus.SYSTEM_ERROR)
        self.assertEqual(result.data, {"error_code": "CONNECTOR_UNAVAILABLE", "connector": "alarms"})
        self.assertIn("no substitute data", result.error)
        self.assertNotIn(SECRET, result.error)

    async def test_not_configured_source_is_reported_and_refused(self):
        with patch.object(settings, "clickhouse_host", ""), patch.object(settings, "postgres_host", ""):
            empty = ConnectorHub(FakeAlarms, FakeDevices)
        status = empty.status()
        self.assertEqual(status["alarms"]["state"], NOT_CONFIGURED)
        self.assertFalse(status["alarms"]["configured"])
        with self.assertRaises(ConnectorUnavailable):
            await asyncio.to_thread(empty.alarms.get_alarms, "X")
        self.assertEqual((await empty.probe())["inventory"]["state"], NOT_CONFIGURED)


class FailureHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_prevents_hammering_and_recovers(self):
        clock, attempts = Clock(), []

        def flaky():
            attempts.append(clock.now)
            if len(attempts) < 4:
                raise OSError("refused")
            return FakeAlarms()

        shared = hub(alarm_factory=flaky, clock=clock)
        call = lambda: shared.alarms.get_alarms("X")  # noqa: E731
        with self.assertRaises(ConnectorUnavailable):
            call()
        with self.assertRaises(ConnectorUnavailable):
            call()  # inside the cooldown: no new connection attempt
        self.assertEqual(len(attempts), 1)
        clock.now += 1.1
        with self.assertRaises(ConnectorUnavailable):
            call()
        clock.now += 1.5  # second delay is 2s, so still cooling down
        with self.assertRaises(ConnectorUnavailable):
            call()
        self.assertEqual(len(attempts), 2)
        clock.now += 1
        with self.assertRaises(ConnectorUnavailable):
            call()
        clock.now += 4.1
        self.assertEqual(call()[1], 1)
        self.assertEqual(shared.status()["alarms"]["state"], HEALTHY)
        self.assertIsNone(shared.status()["alarms"]["error_code"])

    async def test_backoff_is_capped(self):
        clock = Clock()
        shared = hub(alarm_factory=down, clock=clock)
        for _ in range(12):
            clock.now += 100
            with self.assertRaises(ConnectorUnavailable):
                shared.alarms.get_alarms("X")
        self.assertLessEqual(shared.alarms._retry_at - clock.now, connectors.BACKOFF_MAX)

    async def test_connection_loss_drops_the_client_but_query_errors_do_not(self):
        clock = Clock()
        shared = hub(clock=clock)
        FakeAlarms.instances.clear()
        shared.alarms.get_alarms("X")
        client = FakeAlarms.instances[-1]
        client.get_alarms = MagicMock(side_effect=ValueError("bad sql"))
        with self.assertRaises(ValueError):
            shared.alarms.get_alarms("X")
        self.assertEqual(shared.status()["alarms"]["state"], HEALTHY)
        self.assertFalse(client.closed)
        client.get_alarms = MagicMock(side_effect=TimeoutError("slow"))
        with self.assertRaises(ConnectorUnavailable):
            shared.alarms.get_alarms("X")
        self.assertTrue(client.closed)
        self.assertEqual(shared.status()["alarms"]["state"], UNAVAILABLE)
        clock.now += 2
        shared.alarms.get_alarms("X")
        self.assertIsNot(FakeAlarms.instances[-1], client)
        self.assertEqual(shared.status()["alarms"]["state"], HEALTHY)

    async def test_status_exposes_only_the_error_type(self):
        shared = hub(alarm_factory=down)
        with self.assertRaises(ConnectorUnavailable):
            shared.alarms.get_alarms("X")
        status = shared.status()["alarms"]
        self.assertEqual(status["error_code"], "OSError")
        self.assertIsNotNone(status["checked_at"])
        self.assertNotIn(SECRET, json.dumps(shared.status()))

    async def test_initial_state_is_unknown_until_measured(self):
        shared = hub()
        self.assertEqual(shared.status()["alarms"]["state"], UNKNOWN)
        self.assertIsNone(shared.status()["alarms"]["checked_at"])

    async def test_hanging_connect_is_bounded_by_a_deadline(self):
        class Hangs(FakeDevices):
            async def connect(self):
                await asyncio.sleep(30)

        shared = hub(device_factory=Hangs)
        with patch.object(connectors, "CONNECT_DEADLINE", 0.05):
            with self.assertRaises(ConnectorUnavailable):
                await asyncio.wait_for(shared.devices.find_device_candidates(device_name="x"), 5)
        self.assertEqual(shared.status()["inventory"]["state"], UNAVAILABLE)

    async def test_concurrent_first_use_connects_once(self):
        FakeAlarms.instances.clear()
        shared = hub()
        await asyncio.gather(
            *(asyncio.to_thread(shared.alarms.get_alarms, "X") for _ in range(8))
        )
        self.assertEqual(len(FakeAlarms.instances), 1)


class HealthReportingTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_measures_stale_sources_and_skips_fresh_ones(self):
        FakeAlarms.instances.clear()
        shared = hub()
        status = await shared.probe()
        self.assertEqual((status["alarms"]["state"], status["inventory"]["state"]), (HEALTHY, HEALTHY))
        self.assertEqual(len(FakeAlarms.instances), 1)
        checked = status["alarms"]["checked_at"]
        again = await shared.probe(max_age=60)
        self.assertEqual(again["alarms"]["checked_at"], checked)

    async def test_probe_reports_a_dead_source_without_hiding_the_live_one(self):
        shared = hub(alarm_factory=down)
        status = await shared.probe()
        self.assertEqual(status["alarms"]["state"], UNAVAILABLE)
        self.assertEqual(status["inventory"]["state"], HEALTHY)

    async def test_ready_stays_ready_but_degraded_when_a_source_is_down(self):
        import main
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine("sqlite+aiosqlite://")
        self.addAsyncCleanup(engine.dispose)
        main.app.state.ready, main.app.state.worker_task = True, None
        main.app.state.hub = hub(alarm_factory=down)
        with patch("repositories.engine.engine", engine):
            body = await main.ready()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["sources"]["alarms"]["state"], UNAVAILABLE)
        self.assertEqual(body["sources"]["inventory"]["state"], HEALTHY)
        self.assertNotIn(SECRET, json.dumps(body))
        main.app.state.hub = hub()
        with patch("repositories.engine.engine", engine):
            self.assertEqual((await main.ready())["status"], "ready")

    async def test_connectors_endpoint_requires_a_role_and_reports_sources(self):
        from fastapi import HTTPException
        from controllers.operations import connectors as endpoint

        request = NS(app=NS(state=NS(hub=hub(alarm_factory=down))))
        token = _principal.set(Principal("nobody", frozenset({"guest"}), frozenset(), frozenset()))
        with self.assertRaises(HTTPException) as raised:
            await endpoint(request)
        self.assertEqual(raised.exception.status_code, 403)
        _principal.reset(token)
        token = _principal.set(Principal("viewer", frozenset({"viewer"}), frozenset(), frozenset()))
        self.addCleanup(_principal.reset, token)
        body = await endpoint(request)
        self.assertEqual(body["sources"]["alarms"]["state"], UNAVAILABLE)
        self.assertEqual(body["sources"]["inventory"]["state"], HEALTHY)
        self.assertEqual((await endpoint(NS(app=NS(state=NS()))))["sources"], {})

    async def test_close_releases_only_created_clients(self):
        FakeAlarms.instances.clear()
        shared = hub()
        await shared.close()  # nothing connected yet
        shared.alarms.get_alarms("X")
        await shared.devices.find_device_candidates(device_name="x")
        device_client = shared.devices._client
        await shared.close()
        self.assertTrue(FakeAlarms.instances[-1].closed)
        self.assertTrue(device_client.closed)


if __name__ == "__main__":
    unittest.main()
