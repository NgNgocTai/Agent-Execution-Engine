"""Backend-owned, site-scoped data capabilities.

Every capability resolves the site to one canonical station, authorizes the
principal for that station BEFORE any alarm or device query, then runs fixed
parameterized read-only templates with row limits. Not found, ambiguous,
forbidden, source-unavailable and no-data are different outcomes. Sandboxed
skills never receive credentials, SQL or network: the broker below hands them
bounded JSON records produced by these same capabilities.
"""

from datetime import datetime, timezone
import logging

from core.auth import principal

logger = logging.getLogger(__name__)

MAX_DEVICES = 500
CAPABILITIES = ("site_inventory", "site_alarms")


class SiteError(Exception):
    code = "SITE_ERROR"

    def __init__(self, message, **detail):
        super().__init__(message)
        self.detail = detail


class SiteNotFound(SiteError):
    code = "SITE_NOT_FOUND"


class SiteAmbiguous(SiteError):
    code = "SITE_AMBIGUOUS"


class SiteForbidden(SiteError):
    code = "SITE_FORBIDDEN"


def site_allowed(actor, station):
    if "admin" in actor.roles or "*" in actor.sites:
        return True
    return bool(
        {str(station["station_id"]).lower(), str(station["name"]).lower()}
        & actor.sites
    )


async def resolve_site(devices, key, actor=None):
    """Return the one station the actor may read, or raise a distinct SiteError."""
    actor = actor or principal()
    key = (key or "").strip()
    if not key or len(key) > 64:
        raise SiteNotFound("A site id or name is required")
    stations = await devices.find_stations(key)
    if not stations:
        raise SiteNotFound("Site was not found in the inventory")
    allowed = [s for s in stations if site_allowed(actor, s)]
    if not allowed:
        raise SiteForbidden("This principal is not authorized for that site")
    if len(allowed) > 1:
        raise SiteAmbiguous(
            "Several sites match; use an exact station id",
            candidates=[
                {"station_id": s["station_id"], "name": s["name"]} for s in allowed[:20]
            ],
        )
    return allowed[0]


def _provenance(*sources):
    return {
        "sources": list(sources),
        "queried_at": datetime.now(timezone.utc).isoformat(),
        # Alarm times are source-reported event times; retrieval time proves
        # nothing about freshness.
        "observed_at": None,
    }


async def site_inventory(hub, key, limit=200, actor=None):
    actor = actor or principal()
    station = await resolve_site(hub.devices, key, actor)
    limit = max(1, min(int(limit), MAX_DEVICES))
    rows = await hub.devices.list_station_devices(station["station_id"], limit + 1)
    devices = [
        {
            "device_id": row["device_id"],
            "name": row["name"],
            "device_type": row["device_type"],
            "ip_address": row["ip_address"],
            "vendor": row.get("vendor_name"),
        }
        for row in rows[:limit]
    ]
    return {
        "site": {"station_id": station["station_id"], "name": station["name"]},
        "devices": devices,
        "device_count": len(devices),
        "truncated": len(rows) > limit,
        "data_status": "ok" if devices else "no_devices",
        "provenance": _provenance("inventory"),
    }


async def site_alarms(hub, key, limit=50, actor=None):
    actor = actor or principal()
    inventory = await site_inventory(hub, key, MAX_DEVICES, actor)
    limit = max(1, min(int(limit), 100))
    result = {
        "site": inventory["site"],
        "device_count": inventory["device_count"],
        "device_scope_truncated": inventory["truncated"],
        "alarms": [],
        "active_alarm_count": 0,
        "alarms_truncated": False,
        "provenance": _provenance("inventory", "alarms"),
    }
    if not inventory["devices"]:
        return {**result, "data_status": "no_devices"}
    # Authorization already happened in resolve_site; only that station's
    # device ids reach the alarm query.
    ids = [device["device_id"] for device in inventory["devices"]]
    alarms, total = await _alarms(hub, ids, limit)
    return {
        **result,
        "alarms": alarms,
        "active_alarm_count": total,
        "alarms_truncated": total > len(alarms),
        "data_status": "ok" if total else "no_active_alarms",
    }


async def _alarms(hub, ids, limit):
    import asyncio

    return await asyncio.to_thread(
        hub.alarms.get_active_alarms_by_devices, ids, limit
    )


class CapabilityBroker:
    """Supplies reviewed scripts with bounded JSON records, nothing else."""

    def __init__(self, hub):
        self.hub = hub

    async def fetch(self, capability, arguments, max_rows):
        if capability not in CAPABILITIES:
            raise ValueError("Unknown data capability")
        site, rows_limit = arguments.get("site", ""), max(1, int(max_rows))
        if capability == "site_inventory":
            data = await site_inventory(self.hub, site, rows_limit)
            return data["devices"], data
        data = await site_alarms(self.hub, site, rows_limit)
        return data["alarms"], data
