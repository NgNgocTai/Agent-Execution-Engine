import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from pydantic import ValidationError
from core.auth import Principal, _principal, authenticate
from core.config import settings
from services.connectors import ConnectorUnavailable, ConnectorHub
from services.site_capabilities import (
    MAX_DEVICES,
    CapabilityBroker,
    SiteAmbiguous,
    SiteForbidden,
    SiteNotFound,
    resolve_site,
    site_alarms,
    site_inventory,
)
from services.skill_catalog import ScriptSkill
from services.skill_pipeline import manifest_hash
from services.skill_validation import ScriptSpec, inspect_archive
from services.skills.base import SkillStatus
from services.skills.registry import SkillRegistry
from services.skills.site_capabilities import SiteAlarmOverviewSkill, SiteInventorySkill
from services.tool_executor import ToolExecutor

STATIONS = [
    {"station_id": "STN-A", "name": "Hanoi-A"},
    {"station_id": "STN-B", "name": "Hanoi-B"},
    {"station_id": "STN-C1", "name": "Dup"},
    {"station_id": "STN-C2", "name": "dup"},
    {"station_id": "STN-E", "name": "Empty"},
]
DEVICES = {
    "STN-A": [{"device_id": f"a{i}", "name": f"r{i}", "device_type": "router", "ip_address": f"10.0.0.{i}", "vendor_name": "V"} for i in range(3)],
    "STN-B": [{"device_id": "b1", "name": "bx", "device_type": "switch", "ip_address": "10.1.0.1", "vendor_name": "V"}],
    "STN-E": [],
}
ALARMS = {"a0": [{"alarm_id": 1}], "b1": [{"alarm_id": 9}]}


class Devices:
    def __init__(self):
        self.calls = []
        self.connected = True

    async def connect(self):
        pass

    async def close(self):
        pass

    async def find_stations(self, key):
        self.calls.append(("find", key))
        return [s for s in STATIONS if s["station_id"] == key or s["name"].lower() == key.lower()]

    async def list_station_devices(self, station_id, limit):
        self.calls.append(("devices", station_id, limit))
        return DEVICES.get(station_id, [])[:limit]


class Alarms:
    calls = []

    def get_active_alarms_by_devices(self, ids, limit):
        Alarms.calls.append((list(ids), limit))
        rows = [a for i in ids for a in ALARMS.get(i, [])]
        return rows[:limit], len(rows)

    def close(self):
        pass


def make_hub(devices=None, alarms=Alarms):
    devices = devices or Devices()
    with patch.object(settings, "clickhouse_host", "ch"), patch.object(settings, "postgres_host", "pg"):
        hub = ConnectorHub(alarms, lambda: devices)
    return hub, devices


def actor(*sites, roles=("operator",)):
    return Principal("p", frozenset(roles), frozenset(), frozenset(), frozenset(sites))


class ResolutionAndScopeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Alarms.calls = []

    async def test_site_resolves_by_exact_id_or_case_insensitive_name_only(self):
        hub, _ = make_hub()
        me = actor("*")
        self.assertEqual((await resolve_site(hub.devices, "STN-A", me))["name"], "Hanoi-A")
        self.assertEqual((await resolve_site(hub.devices, "hanoi-b", me))["station_id"], "STN-B")
        for key in ("Hanoi", "STN-", "%", "", "x" * 65):
            with self.subTest(key), self.assertRaises(SiteNotFound):
                await resolve_site(hub.devices, key, me)

    async def test_unauthorized_site_is_refused_before_any_device_or_alarm_query(self):
        hub, devices = make_hub()
        with self.assertRaises(SiteForbidden):
            await site_alarms(hub, "STN-B", actor=actor("stn-a"))
        with self.assertRaises(SiteForbidden):
            await site_inventory(hub, "Hanoi-B", actor=actor("stn-a"))
        self.assertEqual([c[0] for c in devices.calls], ["find", "find"])
        self.assertEqual(Alarms.calls, [])

    async def test_scope_matches_station_id_or_name_and_admin_or_wildcard_see_all(self):
        hub, _ = make_hub()
        self.assertEqual((await site_inventory(hub, "STN-A", actor=actor("stn-a")))["device_count"], 3)
        self.assertEqual((await site_inventory(hub, "STN-A", actor=actor("hanoi-a")))["device_count"], 3)
        self.assertEqual((await site_inventory(hub, "STN-B", actor=actor("*")))["device_count"], 1)
        self.assertEqual((await site_inventory(hub, "STN-B", actor=actor(roles=("admin",))))["device_count"], 1)
        with self.assertRaises(SiteForbidden):
            await site_inventory(hub, "STN-A", actor=actor())  # no site scope at all

    async def test_principal_a_never_reads_site_b_data(self):
        hub, _ = make_hub()
        data = await site_alarms(hub, "Hanoi-A", actor=actor("stn-a"))
        self.assertEqual([a["alarm_id"] for a in data["alarms"]], [1])
        self.assertEqual(Alarms.calls[-1][0], ["a0", "a1", "a2"])
        self.assertNotIn("b1", Alarms.calls[-1][0])

    async def test_ambiguity_lists_only_sites_the_principal_may_see(self):
        hub, _ = make_hub()
        with self.assertRaises(SiteAmbiguous) as raised:
            await resolve_site(hub.devices, "dup", actor("stn-c1", "stn-c2"))
        self.assertEqual(
            [c["station_id"] for c in raised.exception.detail["candidates"]], ["STN-C1", "STN-C2"]
        )
        only_one = await resolve_site(hub.devices, "dup", actor("stn-c1"))
        self.assertEqual(only_one["station_id"], "STN-C1")


class DistinctOutcomesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Alarms.calls = []
        token = _principal.set(actor("stn-a", "stn-e", "stn-b"))
        self.addCleanup(_principal.reset, token)

    def registry(self, hub):
        registry = SkillRegistry()
        registry.register(SiteAlarmOverviewSkill(hub))
        registry.register(SiteInventorySkill(hub))
        return ToolExecutor(registry)

    async def test_each_outcome_is_distinguishable(self):
        hub, _ = make_hub()
        executor = self.registry(hub)
        ok = await executor.execute("site_alarm_overview", {"site": "STN-A"})
        self.assertEqual((ok.status, ok.data["data_status"], ok.data["active_alarm_count"]), (SkillStatus.SUCCESS, "ok", 1))
        none = await executor.execute("site_alarm_overview", {"site": "STN-B", "limit": 5})
        self.assertEqual(none.data["active_alarm_count"], 1)
        no_devices = await executor.execute("site_alarm_overview", {"site": "STN-E"})
        self.assertEqual((no_devices.status, no_devices.data["data_status"]), (SkillStatus.SUCCESS, "no_devices"))
        missing = await executor.execute("site_alarm_overview", {"site": "NOPE"})
        forbidden = await executor.execute("site_alarm_overview", {"site": "STN-C1"})
        self.assertEqual(missing.data["error_code"], "SITE_NOT_FOUND")
        self.assertEqual(forbidden.data["error_code"], "SITE_FORBIDDEN")
        self.assertEqual({r.status for r in (missing, forbidden)}, {SkillStatus.BUSINESS_ERROR})
        self.assertEqual(len({ok.data["data_status"], no_devices.data["data_status"], missing.data["error_code"], forbidden.data["error_code"]}), 4)

    async def test_no_active_alarms_differs_from_a_source_outage(self):
        class Quiet(Alarms):
            def get_active_alarms_by_devices(self, ids, limit):
                return [], 0

        class Down:
            def __init__(self):
                raise OSError("refused")

        executor = self.registry(make_hub(alarms=Quiet)[0])
        quiet = await executor.execute("site_alarm_overview", {"site": "STN-A"})
        self.assertEqual((quiet.status, quiet.data["data_status"]), (SkillStatus.SUCCESS, "no_active_alarms"))
        outage = await self.registry(make_hub(alarms=Down)[0]).execute("site_alarm_overview", {"site": "STN-A"})
        self.assertEqual(outage.status, SkillStatus.SYSTEM_ERROR)
        self.assertEqual(outage.data["error_code"], "CONNECTOR_UNAVAILABLE")

    async def test_ambiguous_site_returns_candidates_and_provenance_is_attached(self):
        token = _principal.set(actor("stn-c1", "stn-c2", "stn-a"))
        self.addCleanup(_principal.reset, token)
        executor = self.registry(make_hub()[0])
        ambiguous = await executor.execute("site_inventory", {"site": "dup"})
        self.assertEqual(ambiguous.data["error_code"], "SITE_AMBIGUOUS")
        self.assertEqual(len(ambiguous.data["candidates"]), 2)
        ok = await executor.execute("site_inventory", {"site": "STN-A"})
        self.assertEqual(ok.data["provenance"]["sources"], ["inventory"])
        self.assertIsNone(ok.data["provenance"]["observed_at"])
        self.assertEqual(ok.evidence["entity"], "STN-A")
        self.assertEqual(ok.evidence["source"], "inventory")

    async def test_restricted_principal_cannot_use_aggregate_tools_but_can_use_site_tools(self):
        from services.skills.alarm_statistics import AlarmStatisticsSkill

        hub, _ = make_hub()
        registry = SkillRegistry()
        registry.register(SiteInventorySkill(hub))
        registry.register(AlarmStatisticsSkill(hub.alarms, hub.devices))
        executor = ToolExecutor(registry)
        self.assertTrue((await executor.execute("site_inventory", {"site": "STN-A"})).success)
        denied = await executor.execute("alarm_statistics", {})
        self.assertEqual(denied.data["error_code"], "POLICY_DENIED")

    async def test_limits_bound_rows_and_flag_truncation(self):
        many = {"STN-A": [{"device_id": f"x{i}", "name": "n", "device_type": "t", "ip_address": "1", "vendor_name": "V"} for i in range(MAX_DEVICES + 40)]}
        with patch.dict(DEVICES, many):
            hub, devices = make_hub()
            data = await site_inventory(hub, "STN-A", 10**6, actor=actor("*"))
            self.assertEqual(data["device_count"], MAX_DEVICES)
            self.assertTrue(data["truncated"])
            self.assertEqual(devices.calls[-1][2], MAX_DEVICES + 1)
            alarms = await site_alarms(hub, "STN-A", 10**6, actor=actor("*"))
            self.assertTrue(alarms["device_scope_truncated"])
            self.assertEqual(len(Alarms.calls[-1][0]), MAX_DEVICES)
            self.assertEqual(Alarms.calls[-1][1], 100)


class PrincipalSitesTest(unittest.TestCase):
    def test_sites_are_loaded_lowercased_from_configured_principals(self):
        token = "secret-token"
        entry = {
            "subject": "noc-a",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "roles": ["operator"],
            "sites": ["STN-A", "Hanoi-A"],
        }
        with patch.object(settings, "auth_principals_json", json.dumps([entry])):
            self.assertEqual(authenticate(token).sites, frozenset({"stn-a", "hanoi-a"}))
        with patch.object(settings, "auth_principals_json", json.dumps([{**entry, "sites": "STN-A"}])):
            with self.assertRaises(ValueError):
                settings.validate_security()
        with self.assertRaises(HTTPException):
            authenticate("wrong")


def spec(**overrides):
    base = {
        "entrypoint": "scripts/triage.py",
        "input_fields": [
            {"name": "site", "kind": "string"},
            {"name": "alarms", "kind": "array", "shape": {"type": "array", "maxItems": 50, "items": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}}},
        ],
        "sample_input": {"site": "STN-A", "alarms": [{"alarm_id": 1}]},
        "output_contract": "json",
        "capability": "site_alarms",
        "capability_field": "alarms",
    }
    return {**base, **overrides}


class BrokerSpecTest(unittest.TestCase):
    def test_capability_declaration_rules(self):
        ScriptSpec.model_validate(spec())
        bad = {
            "capability without field": {"capability_field": None},
            "field without capability": {"capability": None},
            "unknown capability": {"capability": "raw_sql"},
            "field is not an array": {"capability_field": "site"},
            "field missing": {"capability_field": "nothing"},
            "no site input": {"input_fields": spec()["input_fields"][1:], "sample_input": {"alarms": []}},
            "optional field": {"input_fields": [spec()["input_fields"][0], {**spec()["input_fields"][1], "required": False}]},
        }
        for name, change in bad.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                ScriptSpec.model_validate(spec(**change))

    def test_agent_never_supplies_the_brokered_records(self):
        model = ScriptSpec.model_validate(spec()).tool_input_model()
        self.assertEqual(set(model.model_json_schema()["properties"]), {"site"})
        with self.assertRaises(ValidationError):
            model.model_validate({"site": "STN-A", "alarms": [{"alarm_id": 99}]})
        self.assertEqual(
            set(ScriptSpec.model_validate(spec()).input_model().model_json_schema()["properties"]),
            {"site", "alarms"},
        )

    def test_capability_is_part_of_the_reviewed_hash(self):
        from services.skill_validation import Manifest

        def manifest(**change):
            return Manifest.model_validate({"name": "custom_x", "version": "1.0.0", "description": "Site alarm triage", "scripts": [spec(**change)]})

        base = manifest()
        self.assertNotEqual(manifest_hash(base), manifest_hash(manifest(capability="site_inventory")))


class BrokeredExecutionTest(unittest.IsolatedAsyncioTestCase):
    def package(self, capability=True):
        from test_skill_pipeline import markdown_package

        runtime = {"scripts": [{**spec(), **({} if capability else {"capability": None, "capability_field": None, "input_fields": [{"name": "site", "kind": "string"}], "sample_input": {"site": "x"}})}]}
        payload = markdown_package({"runtime.json": json.dumps(runtime), "scripts/triage.py": "print('{}')"})
        manifest, _, digest = inspect_archive(payload)
        return NS(id="v1", content_hash=digest, archive=base64.b64encode(payload).decode(), manifest=manifest.model_dump_json(), validation_report=None, status="approved")

    def skill(self, hub, record=None):
        record = record or self.package()
        class Sessions:
            def __call__(self):
                return self

            async def __aenter__(self):
                return NS(get=AsyncMock(return_value=record))

            async def __aexit__(self, *exc):
                return False

        return ScriptSkill(record, Sessions(), None, 2, CapabilityBroker(hub) if hub else None)

    async def run_skill(self, skill, arguments, who):
        token = _principal.set(who)
        self.addCleanup(_principal.reset, token)
        captured = {}

        async def sandbox(manifest, files, args):
            captured.update(args)
            from services.skills.base import SkillResult

            return SkillResult(status=SkillStatus.SUCCESS, data={"ok": True})

        with patch("services.skill_catalog.execute_sandbox", sandbox):
            executor = ToolExecutor(self.registry_with(skill))
            return await executor.execute(skill.name, arguments), captured

    def registry_with(self, skill):
        registry = SkillRegistry()
        registry.register(skill)
        return registry

    async def test_script_receives_only_authorized_records_as_json(self):
        hub, _ = make_hub()
        skill = self.skill(hub)
        self.assertEqual(set(skill.get_tool_schema().parameters["properties"]), {"site"})
        result, received = await self.run_skill(skill, {"site": "STN-A"}, actor("stn-a"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(received, {"site": "STN-A", "alarms": [{"alarm_id": 1}]})
        self.assertNotIn("password", json.dumps(received).lower())

    async def test_other_sites_and_failures_are_distinct_and_never_reach_the_script(self):
        hub, _ = make_hub()
        skill = self.skill(hub)
        result, received = await self.run_skill(skill, {"site": "STN-B"}, actor("stn-a"))
        self.assertEqual(result.data["error_code"], "SITE_FORBIDDEN")
        self.assertEqual(received, {})

        class Down:
            def __init__(self):
                raise OSError("refused")

        outage, received = await self.run_skill(self.skill(make_hub(alarms=Down)[0]), {"site": "STN-A"}, actor("stn-a"))
        self.assertEqual(outage.data["error_code"], "CONNECTOR_UNAVAILABLE")
        self.assertEqual(received, {})
        nobroker, received = await self.run_skill(self.skill(None), {"site": "STN-A"}, actor("stn-a"))
        self.assertEqual(nobroker.data["error_code"], "CONNECTOR_UNAVAILABLE")
        self.assertEqual(received, {})

    async def test_scripts_without_a_capability_get_no_data(self):
        hub, devices = make_hub()
        skill = self.skill(hub, self.package(capability=False))
        result, received = await self.run_skill(skill, {"site": "x"}, actor("*", roles=("admin",)))
        self.assertTrue(result.success)
        self.assertEqual(received, {"site": "x"})
        self.assertEqual(devices.calls, [])
        self.assertIsNone(skill.capability)


if __name__ == "__main__":
    unittest.main()
