# Scoring (calibration: uncalibrated-v0)

Severity weights per alarm: critical 40, major 20, minor 8, warning 4, other 0.
Each severity contributes at most 3 alarms' worth of weight so one noisy alarm
type cannot saturate the score alone. Extra points: +10 when alarms span 3 or
more devices, +10 when 3 or more distinct error codes are active. Score is
capped at 100.

Levels: high >= 70, medium >= 35, low > 0, none_observed = no active alarms.

These weights are a starting point chosen without NOC labelled data. Do not
present the level as an operational decision. Replace the weights and bump the
calibration string only after a NOC-reviewed holdout comparison.

Not inputs: KPI series, latency, packet loss, throughput, link or topology data,
maintenance windows. A site under planned maintenance will still score.
