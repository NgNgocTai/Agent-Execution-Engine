import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from core.auth import Principal, _principal
from core.config import settings
from services.connectors import ConnectorHub
from services.skills.base import SkillStatus
from services.skills.registry import SkillRegistry
from services.skills.schema_catalog import SchemaCatalogSkill
from services.tool_executor import ToolExecutor

ROW_SECRET = "row-value-should-never-appear"


class Alarms:
    calls = []

    def describe_tables(self, tables):
        Alarms.calls.append(list(tables))
        return [{"table": "alarm", "column": c, "type": t} for c, t in (("alarm_id", "UInt64"), ("severity", "String"))] if "alarm" in tables else []

    def close(self):
        pass


class Devices:
    def __init__(self):
        self.calls = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def describe_tables(self, tables):
        self.calls.append(list(tables))
        known = {"device": [("device_id", "text", False), ("name", "text", True)], "station": [("station_id", "text", False)], "vendor": [("vendor_id", "text", False)]}
        return [{"table": t, "column": c, "type": ty, "nullable": n} for t in tables for c, ty, n in known.get(t, [])]


class Down:
    def __init__(self):
        raise OSError("refused")


def make(alarms=Alarms, devices=None):
    devices = devices or Devices()
    with patch.object(settings, "clickhouse_host", "ch"), patch.object(settings, "postgres_host", "pg"):
        hub = ConnectorHub(alarms, (lambda: devices) if not isinstance(devices, type) else devices)
    return SchemaCatalogSkill(hub), devices


class SchemaCatalogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Alarms.calls = []
        token = _principal.set(Principal("op", frozenset({"operator"}), frozenset({"*"}), frozenset({"*"}), frozenset()))
        self.addCleanup(_principal.reset, token)

    async def test_describes_only_allow_listed_tables_and_reports_missing_ones(self):
        skill, devices = make()
        result = await skill.execute()
        self.assertEqual(result.status, SkillStatus.SUCCESS)
        alarms, inventory = result.data["sources"]["alarms"], result.data["sources"]["inventory"]
        self.assertEqual(alarms["tables"]["alarm"][0], {"column": "alarm_id", "type": "UInt64"})
        self.assertEqual(set(inventory["tables"]), {"device", "station", "vendor"})
        self.assertEqual(inventory["missing_tables"], ["error"])
        self.assertEqual(Alarms.calls, [["alarm"]])
        self.assertEqual(devices.calls, [["device", "station", "vendor", "error"]])
        self.assertIn("no row data", result.data["note"])

    async def test_one_table_or_one_source_can_be_requested(self):
        skill, devices = make()
        only = await skill.execute(table="station")
        self.assertEqual(list(only.data["sources"]), ["inventory"])
        self.assertEqual(devices.calls, [["station"]])
        self.assertEqual(Alarms.calls, [])
        alarms_only = await skill.execute(source="alarms")
        self.assertEqual(list(alarms_only.data["sources"]), ["alarms"])

    async def test_tables_outside_the_allowlist_are_refused_before_any_query(self):
        skill, devices = make()
        for table in ("pg_authid", "users", "alarm_archive"):
            result = await skill.execute(table=table)
            self.assertEqual(result.status, SkillStatus.BUSINESS_ERROR)
            self.assertEqual(result.data["error_code"], "TABLE_NOT_ALLOWED")
        refused = await skill.execute(source="alarms", table="device")
        self.assertEqual(refused.data["error_code"], "TABLE_NOT_ALLOWED")
        self.assertEqual((devices.calls, Alarms.calls), ([], []))

    async def test_input_rejects_free_text_and_sql(self):
        model = SchemaCatalogSkill(None).input_model
        for bad in ({"table": "device; drop table x"}, {"table": "Device"}, {"source": "everything"}, {"query": "SELECT 1"}):
            with self.subTest(bad), self.assertRaises(ValidationError):
                model.model_validate(bad)

    async def test_one_source_down_still_returns_the_other(self):
        skill, _ = make(alarms=Down)
        result = await skill.execute()
        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertEqual(result.data["sources"]["alarms"]["status"], "unavailable")
        self.assertEqual(result.data["sources"]["inventory"]["status"], "ok")

    async def test_all_sources_down_is_an_error_not_an_empty_schema(self):
        class DeadDevices(Devices):
            async def connect(self):
                raise ConnectionError("refused")

        skill, _ = make(alarms=Down, devices=DeadDevices())
        result = await skill.execute()
        self.assertEqual(result.status, SkillStatus.SYSTEM_ERROR)
        self.assertEqual(result.data["error_code"], "CONNECTOR_UNAVAILABLE")

    async def test_an_empty_allowlist_is_reported_as_not_configured(self):
        skill, devices = make()
        with patch.object(settings, "schema_discovery_postgres_tables", ""):
            result = await skill.execute()
        self.assertEqual(result.data["sources"]["inventory"]["status"], "not_configured")
        self.assertEqual(devices.calls, [])

    async def test_site_restricted_principals_may_read_metadata(self):
        skill, _ = make()
        registry = SkillRegistry()
        registry.register(skill)
        token = _principal.set(Principal("site-a", frozenset({"operator"}), frozenset(), frozenset(), frozenset({"stn-a"})))
        try:
            result = await ToolExecutor(registry).execute("schema_catalog", {"source": "inventory"})
        finally:
            _principal.reset(token)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.evidence["source"], "schema")

    def test_allowlist_settings_only_accept_plain_table_names(self):
        for bad in ("device;drop", "device, station", "Device", "a b"):
            with self.subTest(bad), self.assertRaises(ValidationError):
                type(settings).model_validate({**settings.model_dump(), "schema_discovery_postgres_tables": bad})


@unittest.skipUnless(os.environ.get("APPROVAL_TEST_POSTGRES_URL"), "Disposable PostgreSQL URL is not configured")
class RealPostgresSchemaTest(unittest.IsolatedAsyncioTestCase):
    async def test_information_schema_query_returns_only_requested_tables_of_the_current_schema(self):
        import asyncpg
        from repositories.postgres_repo import DeviceRepository

        dsn = os.environ["APPROVAL_TEST_POSTGRES_URL"].replace("postgresql+asyncpg://", "postgresql://")
        schema = "schema_test_" + uuid4().hex[:12]
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f"CREATE SCHEMA {schema}")
            await admin.execute(f"CREATE TABLE {schema}.device (device_id text NOT NULL, name text)")
            await admin.execute(f"CREATE TABLE {schema}.secret_table (token text)")
            await admin.execute(f"INSERT INTO {schema}.device VALUES ('d1', '{ROW_SECRET}')")
            repo = DeviceRepository()
            repo._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, server_settings={"search_path": schema, "default_transaction_read_only": "on"})
            try:
                columns = await repo.describe_tables(["device", "station"])
            finally:
                await repo._pool.close()
        finally:
            await admin.execute(f"DROP SCHEMA {schema} CASCADE")
            await admin.close()
        self.assertEqual(columns, [
            {"table": "device", "column": "device_id", "type": "text", "nullable": False},
            {"table": "device", "column": "name", "type": "text", "nullable": True},
        ])
        self.assertNotIn(ROW_SECRET, json.dumps(columns))


if __name__ == "__main__":
    unittest.main()
