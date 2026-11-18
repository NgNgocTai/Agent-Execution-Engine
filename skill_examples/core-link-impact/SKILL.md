---
name: core-link-impact
description: Rank the impact of core and transport link alarms (blast radius by sites and services, plus latency, packet loss and throughput) from alarm or KPI records the operator supplies, with explainable drivers and explicit data gaps
metadata:
  version: 1.0.0
---
# Core link impact

Use this skill when an operator gives you link, interface or transport alarm records
(optionally with latency, packet loss and throughput) and wants to know which link is
most urgent, how many sites and services it touches, and what to check next.

1. **The records must come from the operator.** This system has no link or topology
   source connected, and its alarm table has no `link_id`. Never invent, infer or
   "complete" records. If none were supplied, ask for them and do not call the tool.
2. Call `load_skill` for this package, then call the tool with `records` (1 to 200
   objects). Each needs `link_id` and `severity`; add only the fields the operator gave
   (`alarm_id`, `site_id`, `device`, `interface`, `service`, `timestamp`, `latency_ms`,
   `packet_loss_pct`, `throughput_mbps`). Leave out anything you were not given.
3. Report links in the returned order with `impact_level`, `impact_score` and every
   driver; quote `calibration`. Weights are fixed and UNCALIBRATED
   (references/impact-model.md); say the ranking is a triage aid.
4. Counts of sites and services only include what the records name; the `warnings`
   say when they are lower bounds. Do not state a blast radius larger than the data shows.
5. `none_observed` means the supplied records show no alarm weight or KPI breach, not
   that the link is healthy. `insufficient_data` means no conclusion can be drawn.
6. State that the input was operator supplied (`input_source`) and quote
   `newest_timestamp` when present; otherwise say freshness is unknown.
7. Never name a root cause or claim a link failed. Recommendations are checks to perform.
   Relay `warnings` unchanged.
