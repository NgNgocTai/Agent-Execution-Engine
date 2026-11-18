import json

CALIBRATION = "uncalibrated-v0"
WEIGHT = {"critical": 40, "major": 20, "minor": 8, "warning": 4}
PER_SEVERITY_CAP = 3
INTERPRETATION = "Alarm-derived triage hint from fixed uncalibrated weights; not a root cause."


def severity(value):
    text = str(value or "").strip().lower()
    return text if text in WEIGHT else "unknown"


def level(score):
    if score >= 70:
        return "high"
    if score >= 35:
        return "medium"
    return "low" if score > 0 else "none_observed"


with open("args.json", "r", encoding="utf-8") as handle:
    args = json.load(handle)
site, alarms = args["site"], args["alarms"]

counts = {"critical": 0, "major": 0, "minor": 0, "warning": 0, "unknown": 0}
codes, devices, times = {}, set(), []
for alarm in alarms:
    counts[severity(alarm["severity"])] += 1
    codes[alarm["error_code"]] = codes.get(alarm["error_code"], 0) + 1
    devices.add(alarm["device_id"])
    times.append(alarm["time_created"])

warnings = []
if counts["unknown"]:
    warnings.append(str(counts["unknown"]) + " alarm(s) have an unrecognised severity and add no weight")
if len(alarms) >= 100:
    warnings.append("Alarm list reached its 100 record limit; counts are a lower bound")

result = {
    "calibration": CALIBRATION,
    "site": site,
    "alarm_count": len(alarms),
    "distinct_devices": len(devices),
    "severity_counts": counts,
    "interpretation": INTERPRETATION,
    "warnings": warnings,
}
if not alarms:
    result.update(
        data_status="no_alarms",
        score=0,
        impact_level="none_observed",
        drivers=["No active alarms reported for this site"],
    )
else:
    score, drivers = 0.0, []
    for name in ("critical", "major", "minor", "warning"):
        if counts[name]:
            score += WEIGHT[name] * min(counts[name], PER_SEVERITY_CAP)
            drivers.append(str(counts[name]) + " " + name + " alarm(s)")
    if len(devices) >= 3:
        score += 10
        drivers.append(str(len(devices)) + " devices affected")
    if len(codes) >= 3:
        score += 10
        drivers.append(str(len(codes)) + " distinct error codes")
    score = min(score, 100.0)
    top = sorted(codes.items(), key=lambda item: (-item[1], item[0]))[:5]
    result.update(
        data_status="ok",
        score=round(score, 1),
        impact_level=level(score),
        drivers=drivers,
        top_error_codes=[{"error_code": code, "count": count} for code, count in top],
        newest_alarm_at=max(times),
        oldest_alarm_at=min(times),
    )
print(json.dumps(result))
