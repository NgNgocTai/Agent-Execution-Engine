import hashlib
import json
from datetime import datetime, timedelta, timezone

from core.config import settings

# Keys that carry the time an event happened in the source system. Retrieval
# time is never used as an observation time.
EVENT_TIME_KEYS = {"time_created", "observed_at", "timestamp", "event_time"}
AS_OF_KEY = "as_of"  # an explicit "this snapshot reflects the source at ..." time
MAX_NODES, MAX_DEPTH, MAX_FIELDS = 5000, 6, 20
FUTURE_TOLERANCE = timedelta(minutes=2)


def parse_time(value):
    """UTC datetime from an ISO-like string; naive values are taken as UTC."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _scan(data):
    """(event_times, as_of_times, record_count, field_names), bounded."""
    events, as_of, fields, nodes, records = [], [], [], 0, 0
    stack = [(data, 0)]
    while stack and nodes < MAX_NODES:
        node, depth = stack.pop()
        nodes += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if key in EVENT_TIME_KEYS and (when := parse_time(value)):
                    events.append(when)
                elif key == AS_OF_KEY and (when := parse_time(value)):
                    as_of.append(when)
                elif depth < MAX_DEPTH and isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list) and depth < MAX_DEPTH:
            for item in node:
                if isinstance(item, dict):
                    records += 1
                    for key in item:
                        if key not in fields and len(fields) < MAX_FIELDS:
                            fields.append(key)
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
    return events, as_of, records, fields


def observation(data, retrieved):
    """Event-time facts and an honest freshness verdict for a tool result."""
    events, as_of, records, fields = _scan(data)
    warnings, freshness, basis, observed, skew = [], "unknown", None, None, False
    if as_of:
        observed, basis = max(as_of), "source_as_of"
        age = (retrieved - observed).total_seconds()
        if observed > retrieved + FUTURE_TOLERANCE:
            skew = True
            warnings.append("Source time is in the future: clock skew suspected; freshness unknown")
        else:
            freshness = "fresh" if age <= settings.evidence_stale_seconds else "stale"
            if freshness == "stale":
                warnings.append(
                    f"Source snapshot is {int(age)} s old, beyond {settings.evidence_stale_seconds} s"
                )
    elif events:
        observed, basis = max(events), "record_event_time"
        if observed > retrieved + FUTURE_TOLERANCE:
            skew = True
            warnings.append("A record time is in the future: clock skew suspected")
        warnings.append(
            "observed_at is the newest record event time; it does not prove the source is current"
        )
    else:
        warnings.append(
            "No source observation time in the result; retrieval time is not freshness proof"
        )
    return {
        "observed_at": observed.isoformat() if observed else None,
        "observed_basis": basis,
        "oldest_event_at": min(events).isoformat() if events else None,
        "freshness": freshness,
        "clock_skew_suspected": skew,
        "record_count": records,
        "fields": fields,
        "warnings": warnings,
    }


def evidence(skill, arguments, data):
    fixture = type(skill).__module__.startswith("evals.") or any(type(getattr(skill, name, None)).__module__.startswith("evals.") for name in ("alarm_repo", "device_repo"))
    source = {"node_health_remediate": "ssh", "device_inventory_lookup": "inventory+alarms", "alarm_statistics": "alarms+error_catalog", "noc_alarm_enrichment": "alarms+inventory", "site_inventory": "inventory", "site_alarm_overview": "alarms+inventory"}.get(skill.name, "skill")
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    retrieved = datetime.now(timezone.utc)
    facts = observation(data, retrieved)
    return {"evidence_id": "ev_" + hashlib.sha256(payload.encode()).hexdigest()[:20],
            "source": "fixture:" + source if fixture else source,
            "retrieved_at": retrieved.isoformat(),
            **{key: facts[key] for key in ("observed_at", "observed_basis", "oldest_event_at", "freshness", "clock_skew_suspected", "record_count", "fields")},
            "entity": arguments.get("host") or arguments.get("site") or arguments.get("device_id") or arguments.get("ip_address") or arguments.get("device_name"),
            "time_window_minutes": arguments.get("window_min"),
            "warnings": facts["warnings"]}
