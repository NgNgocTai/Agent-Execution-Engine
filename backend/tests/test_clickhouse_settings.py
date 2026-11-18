from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repositories.clickhouse_repo import AlarmRepository


class FakeClient:
    """Records every query's settings; the server may or may not know the setting."""

    def __init__(self, knows_setting=True, inspect_fails=False):
        self.knows, self.inspect_fails, self.calls = knows_setting, inspect_fails, []

    def query(self, sql, parameters=None, settings=None):
        if "system.settings" in sql:
            if self.inspect_fails:
                raise OSError("no access")
            return NS(result_rows=[[1 if self.knows else 0]])
        self.calls.append(settings)
        return NS(result_rows=[[0] * 6])

    def close(self):
        pass


def repo(client):
    with patch("repositories.clickhouse_repo.clickhouse_connect.get_client", return_value=client):
        return AlarmRepository()


class TopKWorkaroundTest(unittest.TestCase):
    def test_setting_is_added_to_every_query_when_the_server_has_it(self):
        client = FakeClient()
        alarms = repo(client)
        alarms.count_alarms("1 = 1", {})
        alarms.get_active_alarms_by_devices(["d1"], 5)
        self.assertTrue(client.calls)
        for settings in client.calls:
            self.assertEqual(settings["use_top_k_dynamic_filtering"], 0)
        # Per-call settings are kept and win on conflict.
        self.assertEqual(client.calls[-1]["max_execution_time"], 10)

    def test_older_servers_never_receive_an_unknown_setting(self):
        client = FakeClient(knows_setting=False)
        alarms = repo(client)
        alarms.count_alarms("1 = 1", {})
        alarms.get_active_alarms_by_devices(["d1"], 5)
        self.assertTrue(all("use_top_k_dynamic_filtering" not in (s or {}) for s in client.calls))

    def test_failing_to_inspect_settings_does_not_break_startup(self):
        client = FakeClient(inspect_fails=True)
        alarms = repo(client)
        alarms.count_alarms("1 = 1", {})
        self.assertIsNone(client.calls[-1])

    def test_a_caller_can_override_the_workaround(self):
        client = FakeClient()
        alarms = repo(client)
        alarms._query("SELECT 1", settings={"use_top_k_dynamic_filtering": 1})
        self.assertEqual(client.calls[-1]["use_top_k_dynamic_filtering"], 1)


if __name__ == "__main__":
    unittest.main()
