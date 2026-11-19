from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.auth import Principal
from core.config import settings
from core.policy import Decision, plan
from services.connectors import ConnectorHub
from services.site_capabilities import site_list
from services.skill_catalog import BUILTIN_SKILL_NAMES
from services.skills.input_models import SiteListInput
from services.skills.site_capabilities import SiteListSkill

STATIONS = [("STN-A", "Hanoi-A", 3), ("STN-B", "Hanoi-B", 9), ("STN-C", "Danang", 9), ("STN-E", "Empty", 0)]


class Devices:
    """Applies `only` the way the SQL does: lower-case id or name, before ordering and limit."""

    def __init__(self):
        self.calls = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def list_stations(self, limit, only=None):
        self.calls.append((limit, only))
        rows = [
            {"station_id": sid, "name": name, "device_count": count}
            for sid, name, count in STATIONS
            if only is None or sid.lower() in only or name.lower() in only
        ]
        rows.sort(key=lambda row: (-row["device_count"], row["station_id"]))
        return rows[:limit], len(rows)


class Alarms:
    def close(self):
        pass


def hub():
    devices = Devices()
    with patch.object(settings, "clickhouse_host", "ch"), patch.object(settings, "postgres_host", "pg"):
        return ConnectorHub(Alarms, lambda: devices), devices


def actor(*sites, roles=("operator",)):
    return Principal("p", frozenset(roles), frozenset(), frozenset(), frozenset(sites))


class SiteListTest(unittest.IsolatedAsyncioTestCase):
    async def test_unrestricted_actor_sees_all_sites_largest_first_with_ties_by_id(self):
        h, devices = hub()
        out = await site_list(h, 3, actor("*"))
        self.assertEqual([s["station_id"] for s in out["sites"]], ["STN-B", "STN-C", "STN-A"])
        self.assertEqual((out["scope"], out["site_count_total"], out["truncated"]), ("all_sites", 4, True))
        self.assertEqual(devices.calls, [(3, None)])
        admin = await site_list(h, 10, actor(roles=("admin",)))
        self.assertEqual((admin["listed"], admin["truncated"]), (4, False))

    async def test_restricted_actor_only_receives_its_sites_and_the_filter_reaches_the_query(self):
        h, devices = hub()
        out = await site_list(h, 10, actor("stn-a", "danang"))
        self.assertEqual({s["station_id"] for s in out["sites"]}, {"STN-A", "STN-C"})
        self.assertEqual(out["scope"], "authorized_sites")
        self.assertEqual(devices.calls[-1], (10, ["danang", "stn-a"]))

    async def test_actor_without_sites_gets_no_query_and_an_explicit_status(self):
        h, devices = hub()
        out = await site_list(h, 10, actor())
        self.assertEqual((out["data_status"], out["sites"]), ("no_authorized_sites", []))
        self.assertEqual(devices.calls, [])

    async def test_limit_is_bounded_and_counts_are_integers(self):
        h, devices = hub()
        out = await site_list(h, 10_000, actor("*"))
        self.assertEqual(devices.calls[-1][0], 100)
        self.assertTrue(all(isinstance(s["device_count"], int) for s in out["sites"]))
        with self.assertRaises(Exception):
            SiteListInput(limit=0)

    async def test_registered_as_a_read_only_builtin_allowed_for_scoped_operators(self):
        self.assertIn("site_list", BUILTIN_SKILL_NAMES)
        skill = SiteListSkill(hub()[0])
        self.assertEqual(skill.name, "site_list")
        with patch("core.policy.principal", return_value=actor("stn-a")):
            self.assertEqual(plan("site_list", {}), Decision.ALLOW)
        with patch("core.policy.principal", return_value=actor("stn-a", roles=("viewer",))):
            self.assertEqual(plan("site_list", {}), Decision.DENY)


if __name__ == "__main__":
    unittest.main()
