import logging
import threading
from datetime import datetime, timedelta

import clickhouse_connect

from core.config import settings

logger = logging.getLogger(__name__)


class AlarmRepository:
    def __init__(self) -> None:
        self._query_lock = threading.Lock()
        self.client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
            connect_timeout=5,
            send_receive_timeout=10,
        )
        # ClickHouse 26.x can fail "Cannot parse datetime" in its top-K dynamic filter on
        # ORDER BY time_created ... LIMIT with several selected columns. Disable that
        # optimizer only where the server has the setting (older servers reject unknown ones).
        self._extra_settings = {}
        try:
            known = self.client.query(
                "SELECT count() FROM system.settings WHERE name = 'use_top_k_dynamic_filtering'"
            ).result_rows[0][0]
            if known:
                self._extra_settings = {"use_top_k_dynamic_filtering": 0}
        except Exception:
            logger.warning("Could not inspect ClickHouse settings; using server defaults")

    def get_alarms(self, error_code: str, window_min: int = 10, limit: int = 50) -> tuple[list[dict], int]:
        since = datetime.utcnow() - timedelta(minutes=window_min)
        
        count_query = """
            SELECT count(*) FROM alarm
            WHERE error_code = %(code)s AND time_created >= %(since)s
        """
        
        query = """
            SELECT alarm_id, error_code, device_id,
                   time_created, time_solved, status,
                   severity, raw_log, description
            FROM alarm
            WHERE error_code = %(code)s
              AND time_created >= %(since)s
            ORDER BY time_created DESC
            LIMIT %(limit)s
        """
        try:
            total_count = self._query(count_query, parameters={
                "code": error_code,
                "since": since,
            }).result_rows[0][0]

            result = self._query(query, parameters={
                "code": error_code,
                "since": since,
                "limit": limit,
            })
            alarms = [
                {
                    "alarm_id": row[0],
                    "error_code": row[1],
                    "device_id": row[2],
                    "time_created": str(row[3]),
                    "time_solved": str(row[4]) if row[4] else None,
                    "status": row[5],
                    "severity": row[6],
                    "raw_log": row[7],
                    "description": row[8],
                }
                for row in result.result_rows
            ]
            return alarms, total_count
        except Exception as e:
            logger.error("ClickHouse query error: %s", e)
            raise RuntimeError("Alarm data source unavailable") from e

    def count_alarms(self, where_clause: str, params: dict) -> int:
        query = f"SELECT count(*) FROM alarm WHERE {where_clause}"
        try:
            result = self._query(query, parameters=params)
            return result.result_rows[0][0]
        except Exception as e:
            logger.error("ClickHouse count error: %s", e)
            raise RuntimeError("Alarm data source unavailable") from e

    def group_alarms(
        self, group_col: str, where_clause: str, params: dict, limit: int = 0
    ) -> list[dict]:
        query = f"SELECT {group_col}, count(*) as cnt FROM alarm WHERE {where_clause} GROUP BY {group_col} ORDER BY cnt DESC"
        if limit > 0:
            query += f" LIMIT {limit}"
        try:
            result = self._query(query, parameters=params)
            return [{"key": row[0], "count": row[1]} for row in result.result_rows]
        except Exception as e:
            logger.error("ClickHouse group error: %s", e)
            raise RuntimeError("Alarm data source unavailable") from e

    def get_active_alarms_by_device(self, device_id: str, limit: int = 20) -> list[dict]:
        query = """
            SELECT alarm_id, error_code, severity, time_created, status
            FROM alarm
            WHERE device_id = %(device_id)s AND status = 'active'
            ORDER BY time_created DESC
            LIMIT %(limit)s
        """
        try:
            result = self._query(
                query, parameters={"device_id": device_id, "limit": limit}
            )
            return [
                {
                    "alarm_id": row[0],
                    "error_code": row[1],
                    "severity": row[2],
                    "time_created": str(row[3]),
                    "status": row[4],
                }
                for row in result.result_rows
            ]
        except Exception as e:
            logger.error("ClickHouse active alarms error: %s", e)
            raise RuntimeError("Alarm data source unavailable") from e

    def get_active_alarms_by_devices(self, device_ids: list[str], limit: int) -> tuple[list[dict], int]:
        """Active alarms for an explicit device list: parameterized, bounded."""
        if not device_ids:
            return [], 0
        guard = {"max_execution_time": 10}
        total = self._query(
            "SELECT count(*) FROM alarm "
            "WHERE has(%(ids)s, device_id) AND status = 'active'",
            parameters={"ids": list(device_ids)},
            settings=guard,
        ).result_rows[0][0]
        result = self._query(
            "SELECT alarm_id, error_code, device_id, severity, time_created, status "
            "FROM alarm WHERE has(%(ids)s, device_id) AND status = 'active' "
            "ORDER BY time_created DESC LIMIT %(limit)s",
            parameters={"ids": list(device_ids), "limit": limit},
            settings=guard,
        )
        return [
            {
                "alarm_id": row[0],
                "error_code": row[1],
                "device_id": row[2],
                "severity": row[3],
                "time_created": str(row[4]),
                "status": row[5],
            }
            for row in result.result_rows
        ], total

    def describe_tables(self, tables: list[str]) -> list[dict]:
        """Column metadata of the given tables in the configured database only."""
        result = self._query(
            "SELECT table, name, type FROM system.columns "
            "WHERE database = currentDatabase() AND has(%(tables)s, table) "
            "ORDER BY table, position",
            parameters={"tables": list(tables)},
            settings={"max_execution_time": 10},
        )
        return [{"table": row[0], "column": row[1], "type": row[2]} for row in result.result_rows]

    def _query(self, *args, **kwargs):
        # A ClickHouse HTTP session must not have concurrent queries.
        if not self._query_lock.acquire(timeout=10):
            raise TimeoutError("Alarm data source busy")
        try:
            merged = {**self._extra_settings, **(kwargs.pop("settings", None) or {})}
            if merged:
                kwargs["settings"] = merged
            return self.client.query(*args, **kwargs)
        finally:
            self._query_lock.release()

    def close(self) -> None:
        self.client.close()
