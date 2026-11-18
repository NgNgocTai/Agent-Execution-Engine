"""Lazy, independently failing data-source connectors.

Each source (alarms/ClickHouse, inventory/PostgreSQL) connects on first use with
a bounded deadline and backs off after a connection failure, so one source being
down never blocks startup or tools that do not need it. A failed source raises
ConnectorUnavailable; no substitute or demo data is ever returned. The control
database is not managed here: it stays a hard dependency of the application.
"""

import asyncio
from datetime import datetime, timezone
import logging
import threading
import time

from core.config import settings

logger = logging.getLogger(__name__)

NOT_CONFIGURED, UNKNOWN, HEALTHY, UNAVAILABLE = (
    "not_configured",
    "unknown",
    "healthy",
    "unavailable",
)
BACKOFF_START, BACKOFF_MAX = 1.0, 30.0
CONNECT_DEADLINE = 8.0
PROBE_DEADLINE = 3.0


class ConnectorUnavailable(RuntimeError):
    """A data source cannot be used right now. The message is safe to show."""

    def __init__(self, name, reason):
        self.connector, self.reason = name, reason
        super().__init__(
            f"Data source '{name}' is unavailable ({reason}); no substitute data was used."
        )


def is_connection_failure(exc):
    """Connectivity problems, not bad queries, mark a source unavailable."""
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return True
    return any(
        marker in type(exc).__name__.lower()
        for marker in ("connect", "timeout", "operational", "interface")
    )


class _Connector:
    def __init__(self, name, configured, clock=time.monotonic):
        self.name, self.configured, self.clock = name, bool(configured), clock
        self.state = UNKNOWN if self.configured else NOT_CONFIGURED
        self.checked_at = None
        self.error_code = None
        self._failures = 0
        self._retry_at = 0.0

    def _guard(self):
        if not self.configured:
            raise ConnectorUnavailable(self.name, "not configured")
        if self.clock() < self._retry_at:
            raise ConnectorUnavailable(self.name, "recent connection failure")

    def _ok(self):
        self.state, self.error_code, self._failures = HEALTHY, None, 0
        self.checked_at = datetime.now(timezone.utc)

    def _failed(self, exc):
        self._failures += 1
        self._retry_at = self.clock() + min(
            BACKOFF_START * 2 ** (self._failures - 1), BACKOFF_MAX
        )
        self.state = UNAVAILABLE
        # Only the exception type: messages may contain hosts or credentials.
        self.error_code = type(exc).__name__
        self.checked_at = datetime.now(timezone.utc)
        logger.warning("Data source %s unavailable: %s", self.name, self.error_code)

    def status(self):
        return {
            "configured": self.configured,
            "state": self.state,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "error_code": self.error_code,
        }


class SyncConnector(_Connector):
    """Proxy for a blocking client (call its methods from asyncio.to_thread)."""

    def __init__(self, name, configured, factory, cls, ping, **kwargs):
        super().__init__(name, configured, **kwargs)
        self._factory, self._cls, self._ping = factory, cls, ping
        self._client, self._lock = None, threading.Lock()

    def _get(self):
        with self._lock:
            self._guard()
            if self._client is None:
                try:
                    self._client = self._factory()
                except Exception as exc:
                    self._failed(exc)
                    raise ConnectorUnavailable(self.name, "connection failed") from None
            return self._client

    def _drop(self):
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def __getattr__(self, attribute):
        if attribute.startswith("_") or not hasattr(self._cls, attribute):
            raise AttributeError(attribute)

        def call(*args, **kwargs):
            client = self._get()
            try:
                result = getattr(client, attribute)(*args, **kwargs)
            except Exception as exc:
                if is_connection_failure(exc):
                    self._failed(exc)
                    self._drop()
                    raise ConnectorUnavailable(self.name, "connection lost") from None
                raise
            self._ok()
            return result

        return call

    def probe(self):
        try:
            self._ping(self._get())
        except ConnectorUnavailable:
            return
        except Exception as exc:
            self._failed(exc)
            self._drop()
        else:
            self._ok()

    def close(self):
        self._drop()


class AsyncConnector(_Connector):
    def __init__(self, name, configured, factory, cls, ping, **kwargs):
        super().__init__(name, configured, **kwargs)
        self._factory, self._cls, self._ping = factory, cls, ping
        self._client, self._lock = None, asyncio.Lock()

    async def _get(self):
        async with self._lock:
            self._guard()
            if self._client is None:
                client = self._factory()
                try:
                    async with asyncio.timeout(CONNECT_DEADLINE):
                        await client.connect()
                except Exception as exc:
                    self._failed(exc)
                    raise ConnectorUnavailable(self.name, "connection failed") from None
                self._client = client
            return self._client

    async def _drop(self):
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    def __getattr__(self, attribute):
        if attribute.startswith("_") or not hasattr(self._cls, attribute):
            raise AttributeError(attribute)

        async def call(*args, **kwargs):
            client = await self._get()
            try:
                result = await getattr(client, attribute)(*args, **kwargs)
            except Exception as exc:
                if is_connection_failure(exc):
                    self._failed(exc)
                    await self._drop()
                    raise ConnectorUnavailable(self.name, "connection lost") from None
                raise
            self._ok()
            return result

        return call

    async def probe(self):
        try:
            await self._ping(await self._get())
        except ConnectorUnavailable:
            return
        except Exception as exc:
            self._failed(exc)
            await self._drop()
        else:
            self._ok()

    async def close(self):
        await self._drop()


class ConnectorHub:
    """alarms (ClickHouse) and inventory (PostgreSQL), each isolated."""

    def __init__(self, alarm_factory=None, device_factory=None):
        from repositories.clickhouse_repo import AlarmRepository
        from repositories.postgres_repo import DeviceRepository

        self.alarms = SyncConnector(
            "alarms",
            settings.clickhouse_host,
            alarm_factory or AlarmRepository,
            AlarmRepository,
            lambda repo: repo._query("SELECT 1"),
        )
        self.devices = AsyncConnector(
            "inventory",
            settings.postgres_host,
            device_factory or DeviceRepository,
            DeviceRepository,
            lambda repo: repo._pool.fetchval("SELECT 1"),
        )

    def status(self):
        return {"alarms": self.alarms.status(), "inventory": self.devices.status()}

    async def probe(self, max_age=30.0):
        """Measure sources whose last measurement is older than max_age seconds."""
        now = datetime.now(timezone.utc)

        def stale(connector):
            return connector.configured and (
                connector.checked_at is None
                or (now - connector.checked_at).total_seconds() > max_age
            )

        async def measure(connector):
            try:
                async with asyncio.timeout(PROBE_DEADLINE + CONNECT_DEADLINE):
                    if isinstance(connector, AsyncConnector):
                        await connector.probe()
                    else:
                        await asyncio.to_thread(connector.probe)
            except TimeoutError:
                connector._failed(TimeoutError())

        await asyncio.gather(
            *(measure(c) for c in (self.alarms, self.devices) if stale(c))
        )
        return self.status()

    async def close(self):
        await asyncio.to_thread(self.alarms.close)
        await self.devices.close()
