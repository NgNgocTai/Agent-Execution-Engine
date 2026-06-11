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

    def get_alarms(self, error_code: str, window_min: int = 10) -> list[dict]:
        since = datetime.utcnow() - timedelta(minutes=window_min)
        query = """
            SELECT alarm_id, error_code, device_id,
                   time_created, time_solved, status,
                   severity, raw_log, description
            FROM alarm
            WHERE error_code = %(code)s
              AND time_created >= %(since)s
            ORDER BY time_created DESC
        """
        try:
            result = self.client.query(query, parameters={
                "code": error_code,
                "since": since,
            })
            return [
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
        except Exception as e:
            logger.error("ClickHouse query error: %s", e)
            return []

    def close(self) -> None:
        self.client.close()
