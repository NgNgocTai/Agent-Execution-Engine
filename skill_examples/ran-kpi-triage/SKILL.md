---
name: ran-kpi-triage
description: Rank RAN cell KPI anomalies (availability, drop rate, latency, packet loss, throughput, affected subscribers) from KPI records the operator supplies, with explainable drivers and explicit data gaps
metadata:
  version: 1.0.0
---
# RAN KPI triage

Use this skill when an operator gives you KPI records for one or more RAN cells (an
export, a pasted table, monitoring output) and wants them ranked for triage.

1. **The records must come from the operator.** This system has no connected KPI
   source. Never invent, estimate or "typical-value" a record. If the operator has not
   supplied KPI values, ask for them and do not call the tool.
2. Call `load_skill` for this package, then call the triage tool with `records`
   (1 to 200 objects). Each record needs `cell_id`; add only the KPIs the operator
   actually gave: `availability_pct`, `drop_rate_pct`, `latency_ms`, `packet_loss_pct`,
   `throughput_mbps`, `affected_subscribers`, plus optional `alarm_count`, `site_id`,
   `node`, `observed_at`. Leave out any value you were not given; do not fill 0 or 100.
3. Report cells in the returned order with their `severity`, `score` and every driver,
   and quote `calibration`. Thresholds are fixed and UNCALIBRATED (references/thresholds.md);
   say the ranking is a triage aid.
4. A KPI that was not reported was **not checked**. `normal` means "no reported KPI
   breached a threshold", never "healthy". `insufficient_data` means no conclusion.
5. State that the input was operator supplied (`input_source`) and quote
   `newest_observed_at` when present. If it is absent, say freshness is unknown.
6. Never name a root cause. `recommended_actions` are checks to perform, not diagnoses.
   Alarm, link and topology data are not inputs unless given in the records.
7. Relay `warnings` unchanged (duplicate cells, partial KPIs, truncated list).
