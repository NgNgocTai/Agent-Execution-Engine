import logging

import asyncpg

from core.config import settings

logger = logging.getLogger(__name__)


class DeviceRepository:
    def __init__(self) -> None:
        self.conn_kwargs = {
            "host": settings.postgres_host,
            "port": settings.postgres_port,
            "user": settings.postgres_user,
            "password": settings.postgres_password,
            "database": settings.postgres_db,
        }
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        # Inventory reads only: sessions are read-only with a statement timeout.
        self._pool = await asyncpg.create_pool(
            min_size=1,
            max_size=5,
            timeout=5,
            command_timeout=10,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": "10000",
            },
            **self.conn_kwargs,
        )

    async def find_stations(self, key: str) -> list[dict]:
        """Exact station id or case-insensitive exact name; never a pattern."""
        rows = await self._pool.fetch(
            "SELECT station_id, name FROM station "
            "WHERE station_id = $1 OR lower(name) = lower($1) "
            "ORDER BY station_id LIMIT 21",
            key,
        )
        return [dict(row) for row in rows]

    async def list_station_devices(self, station_id: str, limit: int) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT d.device_id, d.name, d.device_type, d.ip_address, "
            "v.name AS vendor_name FROM device d "
            "JOIN vendor v ON d.vendor_id = v.vendor_id "
            "WHERE d.station_id = $1 ORDER BY d.device_id LIMIT $2",
            station_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def describe_tables(self, tables: list[str]) -> list[dict]:
        """Column metadata of the given tables in the connection's schema only."""
        rows = await self._pool.fetch(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY($1::text[]) "
            "ORDER BY table_name, ordinal_position",
            list(tables),
        )
        return [
            {"table": row["table_name"], "column": row["column_name"], "type": row["data_type"],
             "nullable": row["is_nullable"] == "YES"}
            for row in rows
        ]

    async def find_device_candidates(self, *, device_name="", ip_address="", device_id=""):
        column, value = ("d.device_id", device_id) if device_id else ("d.ip_address", ip_address) if ip_address else ("d.name", device_name)
        rows = await self._pool.fetch(
            "SELECT d.device_id, d.name, d.device_type, d.ip_address, v.name as vendor_name, s.name as station_name "
            "FROM device d JOIN vendor v ON d.vendor_id = v.vendor_id JOIN station s ON d.station_id = s.station_id "
            f"WHERE {column} = $1 ORDER BY d.device_id LIMIT 21", value,
        )
        return [dict(row) for row in rows]

    async def get_error_by_name(self, error_name: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT error_code, name, description, domain, default_severity "
            "FROM error WHERE LOWER(name) = LOWER($1)",
            error_name,
        )
        if row:
            return dict(row)
        return None

    async def get_error_by_code(self, error_code: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT error_code, name, description, domain, default_severity "
            "FROM error WHERE error_code = $1",
            error_code,
        )
        if row:
            return dict(row)
        return None

    async def get_errors_by_domain(self, domain: str) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT error_code, name FROM error WHERE LOWER(domain) = LOWER($1)",
            domain,
        )
        return [dict(r) for r in rows]

    async def get_device_with_details(self, device_id: str | None = None, device_name: str | None = None) -> dict | None:
        if device_id:
            row = await self._pool.fetchrow(
                "SELECT d.device_id, d.name, d.device_type, d.ip_address, "
                "v.name as vendor_name, s.name as station_name "
                "FROM device d "
                "JOIN vendor v ON d.vendor_id = v.vendor_id "
                "JOIN station s ON d.station_id = s.station_id "
                "WHERE d.device_id = $1",
                device_id,
            )
        elif device_name:
            row = await self._pool.fetchrow(
                "SELECT d.device_id, d.name, d.device_type, d.ip_address, "
                "v.name as vendor_name, s.name as station_name "
                "FROM device d "
                "JOIN vendor v ON d.vendor_id = v.vendor_id "
                "JOIN station s ON d.station_id = s.station_id "
                "WHERE d.name = $1",
                device_name,
            )
        else:
            return None

        if row:
            return dict(row)
        return None

    async def get_devices_by_names(self, names: list[str]) -> list[dict]:
        if not names:
            return []
        rows = await self._pool.fetch(
            "SELECT d.device_id, d.name, d.device_type, d.ip_address, "
            "v.name as vendor_name, s.name as station_name "
            "FROM device d "
            "JOIN vendor v ON d.vendor_id = v.vendor_id "
            "JOIN station s ON d.station_id = s.station_id "
            "WHERE d.name = ANY($1) OR d.ip_address = ANY($1)",
            names,
        )
        return [dict(r) for r in rows]

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
