from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.skills.base import SkillStatus
from services.evidence import evidence
from services.skills.alarm_statistics import AlarmStatisticsSkill


class EmptyRepo:
    """An alarm source whose newest record is `newest` (None = empty table)."""

    def __init__(self, newest=None, fails=False, in_window=0):
        self.newest, self.fails, self.in_window = newest, fails, in_window

    def count_alarms(self, where_clause, params):
        return self.in_window

    def group_alarms(self, group_col, where_clause, params, limit=0):
        return []

    def newest_alarm_time(self):
        if self.fails:
            raise RuntimeError("Alarm data source unavailable")
        return self.newest


class NoNewestRepo(EmptyRepo):
    newest_alarm_time = None


class Devices:
    async def get_error_by_code(self, code):
        return None

    async def get_device_with_details(self, **kwargs):
        return None


async def stats(repo):
    return await AlarmStatisticsSkill(repo, Devices()).execute(window_min=1440)


class EmptyWindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_static_source_is_reported_with_its_last_record_and_flagged_stale(self):
        june = datetime(2026, 6, 30, 23, 59, 0)  # naive, as ClickHouse returns it
        result = await stats(EmptyRepo(newest=june))
        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertEqual(result.data["total_alarms"], 0)
        self.assertEqual(result.data["source_latest_alarm"], {"time_created": "2026-06-30T23:59:00+00:00"})
        self.assertIn("không được kết luận là mạng không có alarm", result.data["empty_window_note"])
        record = evidence(AlarmStatisticsSkill(EmptyRepo(), Devices()), {"window_min": 1440}, result.data)
        self.assertEqual(record["observed_at"], "2026-06-30T23:59:00+00:00")
        self.assertTrue(any("day(s) old" in w for w in record["warnings"]), record["warnings"])

    async def test_filters_that_exclude_recent_data_do_not_claim_the_source_stopped(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = await stats(EmptyRepo(newest=recent))
        self.assertNotIn("empty_window_note", result.data)
        self.assertNotIn("source_latest_alarm", result.data)

    async def test_empty_table_and_lookup_failure_still_forbid_a_no_alarm_conclusion(self):
        empty = await stats(EmptyRepo(newest=None))
        self.assertEqual(empty.data["empty_window_note"], "Nguồn alarm không có bản ghi nào.")
        failed = await stats(EmptyRepo(fails=True))
        self.assertEqual(failed.status, SkillStatus.SUCCESS)
        self.assertIn("không được kết luận", failed.data["empty_window_note"])

    async def test_non_empty_windows_and_repos_without_the_lookup_are_unchanged(self):
        busy = await stats(EmptyRepo(newest=datetime(2026, 6, 30), in_window=3))
        self.assertNotIn("empty_window_note", busy.data)
        legacy = await stats(NoNewestRepo())
        self.assertNotIn("empty_window_note", legacy.data)


if __name__ == "__main__":
    unittest.main()
