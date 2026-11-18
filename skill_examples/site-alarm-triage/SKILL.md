---
name: site-alarm-triage
description: Rank the impact of active alarms at one NOC site from backend-supplied alarm records, with explainable drivers and explicit data gaps
metadata:
  version: 1.0.0
---
# Site alarm triage

Use this skill when an operator asks how bad the current alarm situation at one
site is, or which alarm types and devices drive it.

1. Call `load_skill` for this package, then call the triage tool with only the
   `site` (station id or exact name). The backend fetches the site's active
   alarms for the caller's authorized sites and passes them to the script; never
   paste alarm records into the call yourself.
2. Report `impact_level`, `score` and every entry of `drivers`, and quote the
   `calibration` value. The score is a ranking aid from fixed, UNCALIBRATED
   weights (see references/scoring.md); say so.
3. `data_status` is `no_alarms` when the site has no active alarm. That is not
   proof of health: the alarm source may be quiet or behind. State it as
   "no active alarms reported", never "healthy".
4. Never name a root cause. The output is an alarm-derived hint. KPI, latency,
   packet loss and link topology are NOT inputs; say that they were not checked.
5. Relay `warnings` (unknown severities, truncated list) unchanged.
6. Always state `newest_alarm_at`. If the tool evidence reports that the newest record is days
   old, say the data may be stale or stopped and do not describe the site as currently affected.
