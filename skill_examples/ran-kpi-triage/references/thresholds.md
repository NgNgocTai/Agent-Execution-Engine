# Thresholds and scoring (calibration: uncalibrated-v0)

Only KPIs present in a record are scored; a missing KPI contributes nothing and is listed
in the cell's drivers. Contributions (each capped):

| KPI | Triggers at | Contribution |
| --- | --- | --- |
| availability_pct | below 97 | (97 - value) x 8, max 35 |
| drop_rate_pct | 3 or more | value x 7, max 30 |
| latency_ms | 100 or more | (value - 100) / 3 + 18, max 28 |
| packet_loss_pct | 2 or more | value x 9, max 30 |
| throughput_mbps | below 25 (0 counts) | (25 - value) x 1.8, max 22 |
| affected_subscribers | 500 or more | value / 45, max 25 |
| alarm_count | 3 or more | value x 3, max 18 |

A reported throughput of exactly 0 Mbps raises the score to at least 50 (major): no traffic is an
outage signal, not a mild anomaly. Score is capped at 100. Severity: critical >= 80, major >= 50, minor >= 25, else normal.
A cell with none of the six KPIs is `insufficient_data` with score 0.

`top_site_by_total_score` sums the scores of a site's cells, so a site with many mildly
degraded cells can outrank one with a single severe cell. It is omitted when no cell scored.

These values were chosen without NOC-labelled data. Do not present severity as an
operational decision. Replace the thresholds and bump the calibration string only after
a NOC-reviewed holdout comparison.

Check hints (not diagnoses): low availability, look at node, sector, power, maintenance;
high drop rate, radio quality, handover, congestion; high latency and loss, backhaul or
queueing; low throughput, PRB utilisation and active users.
