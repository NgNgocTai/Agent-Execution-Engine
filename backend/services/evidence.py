import hashlib
import json
from datetime import datetime, timezone


def evidence(skill, arguments, data):
    fixture = type(skill).__module__.startswith("evals.") or any(type(getattr(skill, name, None)).__module__.startswith("evals.") for name in ("alarm_repo", "device_repo"))
    source = {"node_health_remediate": "ssh", "device_inventory_lookup": "inventory+alarms", "alarm_statistics": "alarms+error_catalog", "noc_alarm_enrichment": "alarms+inventory"}.get(skill.name, "skill")
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return {"evidence_id": "ev_" + hashlib.sha256(payload.encode()).hexdigest()[:20],
            "source": "fixture:" + source if fixture else source,
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "observed_at": None,
            "entity": arguments.get("host") or arguments.get("device_id") or arguments.get("ip_address") or arguments.get("device_name"),
            "time_window_minutes": arguments.get("window_min"),
            "warnings": ["Source observation time is not available; retrieval time is not freshness proof"]}
