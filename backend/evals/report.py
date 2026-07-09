from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any


def write_reports(records: list[dict[str, Any]], results_dir: Path) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)

    json_path = results_dir / "report.json"
    csv_path = results_dir / "report.csv"

    json_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fieldnames = [
        "case_id",
        "name",
        "type",
        "provider",
        "model",
        "comparison_group",
        "passed",
        "failed_checks",
        "warnings",
        "quality_provider",
        "quality_enabled",
        "groundedness_score",
        "correctness_score",
        "latency_ms",
        "tool_calls",
        "approval_events",
        "final_answer",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            quality = record.get("llm_quality", {})
            writer.writerow(
                {
                    "case_id": record.get("case_id"),
                    "name": record.get("name"),
                    "type": record.get("type"),
                    "provider": record.get("provider"),
                    "model": record.get("model"),
                    "comparison_group": record.get("comparison_group"),
                    "passed": record.get("passed"),
                    "failed_checks": "; ".join(record.get("failures", [])),
                    "warnings": "; ".join(
                        record.get("warnings", []) + quality.get("warnings", [])
                    ),
                    "quality_provider": quality.get("provider"),
                    "quality_enabled": quality.get("enabled"),
                    "groundedness_score": quality.get("groundedness_score"),
                    "correctness_score": quality.get("correctness_score"),
                    "latency_ms": record.get("latency_ms"),
                    "tool_calls": len(record.get("tool_calls", [])),
                    "approval_events": len(record.get("approval_events", [])),
                    "final_answer": record.get("final_answer"),
                }
            )


def print_summary(records: list[dict[str, Any]]) -> None:
    executed = len(records)
    passed = sum(1 for item in records if item.get("passed"))
    failed = executed - passed
    latencies = [
        item["latency_ms"]
        for item in records
        if isinstance(item.get("latency_ms"), (int, float))
    ]
    positive_latencies = [value for value in latencies if value > 0]
    avg_latency_s = mean(positive_latencies) / 1000 if positive_latencies else None
    if avg_latency_s is not None and avg_latency_s < 0.01:
        avg_latency_s = None

    groundedness = [
        item["llm_quality"]["groundedness_score"]
        for item in records
        if item.get("llm_quality", {}).get("groundedness_score") is not None
    ]
    correctness = [
        item["llm_quality"]["correctness_score"]
        for item in records
        if item.get("llm_quality", {}).get("correctness_score") is not None
    ]

    print("\n==================================================")
    print("NOC Agent Evaluation Summary")
    print("==================================================\n")
    print(f"Cases executed       : {executed}")
    print(f"Passed               : {passed}")
    print(f"Failed               : {failed}")
    if avg_latency_s is not None:
        print(f"Average latency      : {avg_latency_s:.2f} s")
    if groundedness:
        print(f"Average groundedness : {_mean_or_na(groundedness)}")
    if correctness:
        print(f"Average correctness  : {_mean_or_na(correctness)}")
    print("\n==================================================")


def _mean_or_na(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"{mean(values):.2f}"
