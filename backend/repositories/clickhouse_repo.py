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

    def close(self) -> None:
        self.client.close()
