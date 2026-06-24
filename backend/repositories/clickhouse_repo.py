import logging
from datetime import datetime, timedelta

import clickhouse_connect

from core.config import settings

logger = logging.getLogger(__name__)


class AlarmRepository:
    def __init__(self) -> None:
        self.client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
        )

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
            total_count = self.client.query(count_query, parameters={
                "code": error_code,
                "since": since,
            }).result_rows[0][0]

            result = self.client.query(query, parameters={
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
            return [], 0

    def count_alarms(self, where_clause: str, params: dict) -> int:
        query = f"SELECT count(*) FROM alarm WHERE {where_clause}"
        try:
            result = self.client.query(query, parameters=params)
            return result.result_rows[0][0]
        except Exception as e:
            logger.error("ClickHouse count error: %s", e)
            return 0

    def group_alarms(
        self, group_col: str, where_clause: str, params: dict, limit: int = 0
    ) -> list[dict]:
        query = f"SELECT {group_col}, count(*) as cnt FROM alarm WHERE {where_clause} GROUP BY {group_col} ORDER BY cnt DESC"
        if limit > 0:
            query += f" LIMIT {limit}"
        try:
            result = self.client.query(query, parameters=params)
            return [{"key": row[0], "count": row[1]} for row in result.result_rows]
        except Exception as e:
            logger.error("ClickHouse group error: %s", e)
            return []

    def get_active_alarms_by_device(self, device_id: str, limit: int = 20) -> list[dict]:
        query = """
            SELECT alarm_id, error_code, severity, time_created, status
            FROM alarm
            WHERE device_id = %(device_id)s AND status = 'active'
            ORDER BY time_created DESC
            LIMIT %(limit)s
        """
        try:
            result = self.client.query(
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
            return []

    def close(self) -> None:
        self.client.close()
