import json

CALIBRATION = "uncalibrated-v0"
INTERPRETATION = "KPI-derived triage hint from fixed uncalibrated thresholds; not a root cause."
KPIS = (
    "availability_pct",
    "drop_rate_pct",
    "latency_ms",
    "packet_loss_pct",
    "throughput_mbps",
    "affected_subscribers",
)
AVAILABILITY_TARGET = 97.0
DROP_RATE_MAJOR = 3.0
LATENCY_MAJOR = 100.0
PACKET_LOSS_MAJOR = 2.0
THROUGHPUT_LOW = 25.0
SUBSCRIBERS_MAJOR = 500
MAX_CELLS_OUT = 50
SEVERITIES = ("critical", "major", "minor", "normal", "insufficient_data")


def classify(score):
    if score >= 80:
        return "critical"
    if score >= 50:
        return "major"
    if score >= 25:
        return "minor"
    return "normal"


def analyze(record):
    """Score only the KPIs the record reports; an absent KPI is never assumed healthy."""
    score, drivers, actions = 0.0, [], []
    missing = [name for name in KPIS if name not in record]

    if "availability_pct" in record and record["availability_pct"] < AVAILABILITY_TARGET:
        value = record["availability_pct"]
        score += min((AVAILABILITY_TARGET - value) * 8, 35)
        drivers.append(f"availability below target: {value:.2f}%")
        actions.append("Check RAN node reachability, sector availability and recent maintenance events.")
    if "drop_rate_pct" in record and record["drop_rate_pct"] >= DROP_RATE_MAJOR:
        value = record["drop_rate_pct"]
        score += min(value * 7, 30)
        drivers.append(f"drop rate elevated: {value:.2f}%")
        actions.append("Inspect radio interference, handover failures and admission or congestion counters.")
    if "latency_ms" in record and record["latency_ms"] >= LATENCY_MAJOR:
        value = record["latency_ms"]
        score += min((value - LATENCY_MAJOR) / 3 + 18, 28)
        drivers.append(f"latency elevated: {value:.0f} ms")
        actions.append("Check transport backhaul latency and upstream queueing.")
    if "packet_loss_pct" in record and record["packet_loss_pct"] >= PACKET_LOSS_MAJOR:
        value = record["packet_loss_pct"]
        score += min(value * 9, 30)
        drivers.append(f"packet loss elevated: {value:.2f}%")
        actions.append("Validate backhaul interface errors and transmission path health.")
    # A reported 0 Mbps is a real and severe reading, so it is scored (not skipped as "no data").
    if "throughput_mbps" in record and record["throughput_mbps"] < THROUGHPUT_LOW:
        value = record["throughput_mbps"]
        score += min((THROUGHPUT_LOW - value) * 1.8, 22)
        drivers.append(f"throughput low: {value:.1f} Mbps")
        actions.append("Compare PRB utilisation, scheduler congestion and user-plane throughput trend.")
    if "affected_subscribers" in record and record["affected_subscribers"] >= SUBSCRIBERS_MAJOR:
        value = record["affected_subscribers"]
        score += min(value / 45, 25)
        drivers.append(f"affected subscribers: {value}")
        actions.append("Escalate with subscriber impact and the affected site and cell identifiers.")
    if record.get("alarm_count", 0) >= 3:
        value = record["alarm_count"]
        score += min(value * 3, 18)
        drivers.append(f"co-occurring alarms: {value}")

    reported = len(KPIS) - len(missing)
    if reported == 0:
        severity, score = "insufficient_data", 0.0
        drivers = ["No KPI value was reported for this cell"]
        actions = ["Obtain KPI values for this cell before drawing any conclusion."]
    else:
        score = round(min(score, 100.0), 1)
        severity = classify(score)
        if not drivers:
            drivers = ["No reported KPI breached a configured threshold"]
            actions = ["Keep monitoring and compare against a baseline before escalating."]
        if missing:
            drivers.append(f"{len(missing)} KPI(s) not reported: " + ", ".join(missing))

    cell = {
        "cell_id": record["cell_id"],
        "score": score,
        "severity": severity,
        "kpis_reported": reported,
        "drivers": drivers,
        "recommended_actions": list(dict.fromkeys(actions)),
    }
    for key in ("site_id", "node"):
        if key in record:
            cell[key] = record[key]
    return cell


with open("args.json", "r", encoding="utf-8") as handle:
    args = json.load(handle)
records = args["records"]

cells = [analyze(record) for record in records]
cells.sort(key=lambda item: (-item["score"], item["severity"] == "insufficient_data", item["cell_id"]))

counts = {name: 0 for name in SEVERITIES}
site_totals = {}
for cell in cells:
    counts[cell["severity"]] += 1
    if "site_id" in cell and cell["score"] > 0:
        site_totals[cell["site_id"]] = site_totals.get(cell["site_id"], 0.0) + cell["score"]

warnings = []
ids = [record["cell_id"] for record in records]
duplicates = sorted({cell_id for cell_id in ids if ids.count(cell_id) > 1})
if duplicates:
    warnings.append(
        "Duplicate cell_id kept as separate rows: " + ", ".join(duplicates[:5])
        + (" ..." if len(duplicates) > 5 else "")
    )
if counts["insufficient_data"]:
    warnings.append(str(counts["insufficient_data"]) + " cell(s) reported no KPI and were not scored")
partial = sum(1 for cell in cells if 0 < cell["kpis_reported"] < len(KPIS))
if partial:
    warnings.append(str(partial) + " cell(s) report only some KPIs; missing KPIs were not assumed healthy")
times = [record["observed_at"] for record in records if "observed_at" in record]
untimed = len(records) - len(times)
if untimed:
    warnings.append(str(untimed) + " record(s) have no observed_at; freshness is unknown")
if len(cells) > MAX_CELLS_OUT:
    warnings.append(
        "Only the " + str(MAX_CELLS_OUT) + " highest-scoring of " + str(len(cells)) + " cells are listed; counts cover all"
    )

summary = {"total_cells": len(cells), "severity_counts": counts}
if site_totals:
    summary["top_site_by_total_score"] = sorted(site_totals.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]

result = {
    "calibration": CALIBRATION,
    "input_source": "operator_supplied",
    "interpretation": INTERPRETATION,
    "summary": summary,
    "ranked_cells": cells[:MAX_CELLS_OUT],
    "warnings": warnings,
}
if times:
    result["newest_observed_at"] = max(times)
    result["oldest_observed_at"] = min(times)
print(json.dumps(result))
