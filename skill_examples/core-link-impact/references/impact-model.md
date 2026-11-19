# Impact model (calibration: uncalibrated-v0)

Records are grouped by `link_id`. Per link:

- Severity weight per record: critical 50, major 30, minor 12, warning 8, normal 0.
  Accepted spellings: crit/critical/p1, maj/major/p2, min/minor/p3, warn/warning,
  normal/clear/cleared/ok. Anything else is `unknown`, adds no weight and is flagged.
- More than one distinct service: +10 per service. More than one distinct site: +12 per
  site. Only services and sites named in the records are counted.
- Highest latency 100 ms or more: (value - 100) / 2 + 15, max 35.
- Highest packet loss 2% or more: value x 8, max 35.
- Lowest throughput below 100 Mbps (0 counts): (100 - value) / 4, max 20.

A lowest throughput of exactly 0 Mbps raises the score to at least 45 (medium): a link carrying
nothing is a failure signal. Score is capped at 100. Levels: high >= 80, medium >= 45, low > 0, none_observed = score 0
with usable evidence, insufficient_data = no recognised severity and no KPI.

Unlike the sample-data tools it was modelled on, this skill never substitutes built-in
records. Missing KPIs are not scored, unrecognised severities are not read as normal, and
a 0 Mbps reading is scored as the severe reading it is.

Weights were chosen without NOC-labelled data. Do not present the level as an operational
decision; replace the weights and bump the calibration string only after a NOC-reviewed
holdout comparison.
