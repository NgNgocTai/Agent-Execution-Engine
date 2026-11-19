import json

CALIBRATION = "uncalibrated-v0"
INTERPRETATION = "Alarm- and KPI-derived impact hint from fixed uncalibrated weights; not a root cause."
WEIGHT = {"critical": 50, "major": 30, "minor": 12, "warning": 8, "normal": 0}
ALIASES = {
    "crit": "critical", "critical": "critical", "p1": "critical",
    "maj": "major", "major": "major", "p2": "major",
    "min": "minor", "minor": "minor", "p3": "minor",
    "warn": "warning", "warning": "warning",
    "normal": "normal", "clear": "normal", "cleared": "normal", "ok": "normal",
}
MAX_LINKS_OUT = 50
LIST_LIMIT = 10
LATENCY_HIGH = 100.0
LOSS_HIGH = 2.0
THROUGHPUT_LOW = 100.0
OUTAGE_FLOOR = 45.0  # a reported 0 Mbps is at least "medium"


def severity(value):
    return ALIASES.get(str(value).strip().lower(), "unknown")


def listing(values):
    return sorted(values)[:LIST_LIMIT]


def level(score, usable):
    if score >= 80:
        return "high"
    if score >= 45:
        return "medium"
    if score > 0:
        return "low"
    return "none_observed" if usable else "insufficient_data"


def score_group(group):
    score, drivers = 0.0, []
    for name in ("critical", "major", "minor", "warning"):
        count = group["counts"][name]
        if count:
            score += WEIGHT[name] * count
            drivers.append(f"{count} {name} alarm(s)")
    if len(group["services"]) > 1:
        score += len(group["services"]) * 10
        drivers.append(f"{len(group['services'])} affected services")
    if len(group["sites"]) > 1:
        score += len(group["sites"]) * 12
        drivers.append(f"{len(group['sites'])} impacted sites")
    if group["latency"] is not None and group["latency"] >= LATENCY_HIGH:
        score += min((group["latency"] - LATENCY_HIGH) / 2 + 15, 35)
        drivers.append(f"latency peak {group['latency']:.0f} ms")
    if group["loss"] is not None and group["loss"] >= LOSS_HIGH:
        score += min(group["loss"] * 8, 35)
        drivers.append(f"packet loss peak {group['loss']:.2f}%")
    # A reported 0 Mbps is a real reading (dead path), so it is scored, not skipped.
    if group["throughput"] is not None and group["throughput"] < THROUGHPUT_LOW:
        score += min((THROUGHPUT_LOW - group["throughput"]) / 4, 20)
        drivers.append(f"low throughput {group['throughput']:.1f} Mbps")
    if group["throughput"] == 0:
        # A link carrying nothing is a failure signal, never "low".
        score = max(score, OUTAGE_FLOOR)
        drivers.append("zero throughput: possible link failure, impact floor applied")
    return round(min(score, 100.0), 1), drivers


def actions_for(group, level_name):
    if level_name == "high":
        actions = ["Open an incident bridge and validate link redundancy."]
    elif level_name == "medium":
        actions = ["Escalate to the transport or core team if degradation persists."]
    elif level_name == "low":
        actions = ["Monitor and correlate with nearby site and service alarms."]
    else:
        actions = ["Obtain recognised severities or KPI values before drawing a conclusion."] \
            if level_name == "insufficient_data" else []
    if group["loss"] is not None and group["loss"] >= LOSS_HIGH:
        actions.append("Check interface errors, optical levels and drops on both link endpoints.")
    if group["latency"] is not None and group["latency"] >= LATENCY_HIGH:
        actions.append("Inspect congestion, route changes and queueing on the affected path.")
    return list(dict.fromkeys(actions))


with open("args.json", "r", encoding="utf-8") as handle:
    args = json.load(handle)
records = args["records"]

groups, totals = {}, {"critical": 0, "major": 0, "minor": 0, "warning": 0, "normal": 0, "unknown": 0}
for record in records:
    group = groups.setdefault(record["link_id"], {
        "sites": set(), "devices": set(), "interfaces": set(), "services": set(), "alarm_ids": [],
        "counts": {"critical": 0, "major": 0, "minor": 0, "warning": 0, "normal": 0, "unknown": 0},
        "latency": None, "loss": None, "throughput": None, "kpi_records": 0,
    })
    sev = severity(record["severity"])
    group["counts"][sev] += 1
    totals[sev] += 1
    for source, target in (("site_id", "sites"), ("device", "devices"), ("interface", "interfaces"), ("service", "services")):
        if source in record:
            group[target].add(record[source])
    if "alarm_id" in record:
        group["alarm_ids"].append(record["alarm_id"])
    reported = False
    if "latency_ms" in record:
        reported = True
        group["latency"] = record["latency_ms"] if group["latency"] is None else max(group["latency"], record["latency_ms"])
    if "packet_loss_pct" in record:
        reported = True
        group["loss"] = record["packet_loss_pct"] if group["loss"] is None else max(group["loss"], record["packet_loss_pct"])
    if "throughput_mbps" in record:
        reported = True
        group["throughput"] = record["throughput_mbps"] if group["throughput"] is None else min(group["throughput"], record["throughput_mbps"])
    group["kpi_records"] += reported

impacts = []
for link_id, group in groups.items():
    score, drivers = score_group(group)
    usable = group["kpi_records"] > 0 or any(group["counts"][name] for name in WEIGHT)
    name = level(score, usable)
    if not drivers:
        drivers = (["No alarm or KPI evidence was usable for this link"] if name == "insufficient_data"
                   else ["Only normal or cleared records, no KPI breach reported"])
    if group["counts"]["unknown"]:
        drivers.append(str(group["counts"]["unknown"]) + " record(s) with unrecognised severity added no weight")
    item = {
        "link_id": link_id,
        "impact_score": score,
        "impact_level": name,
        "record_count": sum(group["counts"].values()),
        "severity_counts": group["counts"],
        "site_count": len(group["sites"]), "sites": listing(group["sites"]),
        "service_count": len(group["services"]), "services": listing(group["services"]),
        "devices": listing(group["devices"]),
        "interfaces": listing(group["interfaces"]),
        "alarm_ids": group["alarm_ids"][:LIST_LIMIT],
        "drivers": drivers,
        "recommendations": actions_for(group, name),
    }
    impacts.append(item)
impacts.sort(key=lambda entry: (-entry["impact_score"], entry["impact_level"] == "insufficient_data", entry["link_id"]))

warnings = []
if totals["unknown"]:
    warnings.append(str(totals["unknown"]) + " record(s) have an unrecognised severity and add no weight")
no_site = sum(1 for record in records if "site_id" not in record)
if no_site:
    warnings.append(str(no_site) + " record(s) have no site_id; impacted sites are a lower bound")
no_service = sum(1 for record in records if "service" not in record)
if no_service:
    warnings.append(str(no_service) + " record(s) have no service; affected services are a lower bound")
times = [record["timestamp"] for record in records if "timestamp" in record]
if len(times) < len(records):
    warnings.append(str(len(records) - len(times)) + " record(s) have no timestamp; freshness is unknown")
if len(impacts) > MAX_LINKS_OUT:
    warnings.append(
        "Only the " + str(MAX_LINKS_OUT) + " highest-impact of " + str(len(impacts)) + " links are listed; counts cover all"
    )

result = {
    "calibration": CALIBRATION,
    "input_source": "operator_supplied",
    "interpretation": INTERPRETATION,
    "summary": {
        "total_records": len(records),
        "link_count": len(impacts),
        "severity_counts": totals,
    },
    "impacts": impacts[:MAX_LINKS_OUT],
    "warnings": warnings,
}
if impacts and impacts[0]["impact_score"] > 0:
    result["summary"]["top_link"] = impacts[0]["link_id"]
if times:
    result["newest_timestamp"] = max(times)
    result["oldest_timestamp"] = min(times)
print(json.dumps(result))
