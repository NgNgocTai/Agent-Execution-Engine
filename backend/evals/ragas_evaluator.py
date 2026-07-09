from __future__ import annotations

import asyncio
import json
import math
import os
from numbers import Real
from typing import Any

from evals.collector import EvalCollector

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


async def evaluate_quality(
    case: dict[str, Any],
    collector: EvalCollector,
    evaluator: str = "none",
) -> dict[str, Any]:
    """Evaluate final answer quality without affecting deterministic pass/fail.

    ``none`` keeps benchmark runs deterministic-only. ``ragas`` enables optional
    LLM-as-a-Judge metrics through Ragas and an OpenAI-compatible client such as
    OpenRouter.
    """

    if evaluator == "none":
        return _quality_not_evaluated()

    if evaluator == "ragas":
        return await asyncio.to_thread(_evaluate_with_ragas, case, collector)

    raise ValueError(f"Unknown quality evaluator: {evaluator}")


def _evaluate_with_ragas(case: dict[str, Any], collector: EvalCollector) -> dict[str, Any]:
    final_answer = _final_answer(collector)
    contexts = _tool_observations_as_contexts(collector)
    reference_answer = _reference_answer(case)
    question = case.get("user_input", "")

    if not final_answer:
        return _ragas_skipped("missing_final_answer")
    if not contexts:
        return _ragas_skipped("missing_tool_observations")

    os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")

    try:
        from datasets import Dataset
        from ragas import evaluate
    except Exception as exc:  # pragma: no cover - depends on optional package.
        return _ragas_unavailable(f"Ragas dependency is not available: {exc}")

    try:
        llm, model, base_url = _build_ragas_llm()
        metrics = _build_ragas_metrics(llm, has_reference=bool(reference_answer))
        dataset = Dataset.from_dict(
            {
                "question": [question],
                "answer": [final_answer],
                "contexts": [contexts],
                **({"ground_truth": [reference_answer]} if reference_answer else {}),
            }
        )

        ragas_result = evaluate(
            dataset,
            metrics=metrics,
            llm=llm,
            raise_exceptions=False,
        )
        scores = _extract_scores(ragas_result)
        warnings = _score_warnings(scores)
        return {
            "provider": "ragas",
            "enabled": True,
            "model": model,
            "base_url": base_url,
            "groundedness_score": _first_score(
                scores,
                ["faithfulness", "response_groundedness", "groundedness"],
            ),
            "correctness_score": _first_score(
                scores,
                ["answer_correctness", "factual_correctness", "correctness"],
            ),
            "answer_quality": "evaluated_by_ragas",
            "warnings": warnings,
            "raw_scores": scores,
        }
    except Exception as exc:  # pragma: no cover - network/provider dependent.
        return _ragas_error(f"Ragas evaluation failed: {exc}")


def _build_ragas_llm() -> tuple[Any, str, str | None]:
    from ragas.llms import llm_factory

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    api_key = os.getenv("RAGAS_EVAL_API_KEY") or openrouter_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing RAGAS_EVAL_API_KEY, OPENROUTER_API_KEY, or OPENAI_API_KEY."
        )

    base_url = (
        os.getenv("RAGAS_EVAL_BASE_URL")
        or os.getenv("OPENROUTER_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
    )
    if not base_url and openrouter_key:
        base_url = DEFAULT_OPENROUTER_BASE_URL

    is_openrouter = bool(openrouter_key) or bool(
        base_url and "openrouter" in base_url.lower()
    )
    default_model = DEFAULT_OPENROUTER_MODEL if is_openrouter else DEFAULT_OPENAI_MODEL
    model = os.getenv("RAGAS_EVAL_MODEL", default_model)

    # Ragas 0.3.x uses langchain-openai internally and reads OPENAI_API_KEY.
    # Keep this assignment local to the eval process so RAGAS_EVAL_API_KEY or
    # OPENROUTER_API_KEY can override the app's OpenAI-compatible key.
    os.environ["OPENAI_API_KEY"] = api_key

    headers: dict[str, str] = {}
    referer = os.getenv("RAGAS_EVAL_HTTP_REFERER") or os.getenv("OPENROUTER_HTTP_REFERER")
    app_title = os.getenv("RAGAS_EVAL_APP_TITLE") or os.getenv("OPENROUTER_APP_TITLE")
    if referer:
        headers["HTTP-Referer"] = referer
    if app_title:
        headers["X-Title"] = app_title

    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if headers:
        kwargs["default_headers"] = headers

    return llm_factory(model=model, **kwargs), model, base_url


def _build_ragas_metrics(llm: Any, has_reference: bool) -> list[Any]:
    import ragas.metrics as metric_module

    metrics = [
        _load_metric(metric_module, ["Faithfulness", "faithfulness"], llm),
    ]
    if has_reference:
        metrics.append(
            _load_metric(
                metric_module,
                ["AnswerCorrectness", "answer_correctness", "FactualCorrectness"],
                llm,
            )
        )
    return metrics


def _load_metric(metric_module: Any, names: list[str], llm: Any) -> Any:
    for name in names:
        metric = getattr(metric_module, name, None)
        if metric is None:
            continue
        if isinstance(metric, type):
            try:
                return metric(llm=llm)
            except TypeError:
                return metric()
        return metric
    raise RuntimeError(f"Could not find Ragas metric candidates: {', '.join(names)}")


def _extract_scores(ragas_result: Any) -> dict[str, Any]:
    if hasattr(ragas_result, "to_pandas"):
        frame = ragas_result.to_pandas()
        if not frame.empty:
            row = frame.iloc[0].to_dict()
            return {
                key: _json_safe(value)
                for key, value in row.items()
                if key not in {"question", "answer", "contexts", "ground_truth"}
            }

    if isinstance(ragas_result, dict):
        return {key: _json_safe(value) for key, value in ragas_result.items()}

    return {"result": _json_safe(ragas_result)}


def _first_score(scores: dict[str, Any], names: list[str]) -> float | None:
    for name in names:
        value = scores.get(name)
        if isinstance(value, Real) and not math.isnan(float(value)):
            return float(value)
    return None


def _score_warnings(scores: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key, value in scores.items():
        if isinstance(value, str) and value.lower() in {"nan", "none", "error"}:
            warnings.append(f"Ragas metric {key} returned {value}.")
        if isinstance(value, Real) and math.isnan(float(value)):
            warnings.append(f"Ragas metric {key} returned NaN.")
    return warnings


def _ragas_skipped(reason: str) -> dict[str, Any]:
    return {
        "provider": "ragas",
        "enabled": False,
        "model": os.getenv("RAGAS_EVAL_MODEL", DEFAULT_OPENROUTER_MODEL),
        "groundedness_score": None,
        "correctness_score": None,
        "answer_quality": "skipped",
        "warnings": [reason],
    }


def _ragas_unavailable(reason: str) -> dict[str, Any]:
    result = _quality_not_evaluated()
    result.update(
        {
            "provider": "ragas_unavailable",
            "warnings": [reason],
        }
    )
    return result


def _ragas_error(reason: str) -> dict[str, Any]:
    result = _quality_not_evaluated()
    result.update(
        {
            "provider": "ragas_error",
            "warnings": [reason],
        }
    )
    return result


def _quality_not_evaluated() -> dict[str, Any]:
    return {
        "provider": "none",
        "enabled": False,
        "model": None,
        "groundedness_score": None,
        "correctness_score": None,
        "answer_quality": "not_evaluated",
        "warnings": [],
    }


def _final_answer(collector: EvalCollector) -> str:
    return collector.final_answer or "".join(collector.text_chunks)


def _reference_answer(case: dict[str, Any]) -> str | None:
    answer_spec = case.get("expected", {}).get("answer", {})
    return answer_spec.get("reference_answer") or answer_spec.get("semantic_reference")


def _tool_observations_as_contexts(collector: EvalCollector) -> list[str]:
    contexts = []
    tool_args_by_id = {
        item.get("call_id"): item.get("arguments", {})
        for item in collector.tool_calls
        if item.get("call_id")
    }
    for item in collector.tool_results:
        args = tool_args_by_id.get(item.get("call_id"), {})
        contexts.append(_summarize_tool_observation(item, args))
    return contexts


def _summarize_tool_observation(
    item: dict[str, Any],
    arguments: dict[str, Any],
) -> str:
    tool_name = item.get("tool_name") or "unknown_tool"
    status = item.get("status") or "unknown"
    result = item.get("result")
    error = item.get("error")

    if status != "success" or error:
        return _summarize_error(tool_name, status, arguments, error)

    if tool_name == "noc_alarm_enrichment":
        return _summarize_alarm_enrichment(result, arguments)
    if tool_name == "alarm_statistics":
        return _summarize_alarm_statistics(result, arguments)
    if tool_name == "device_inventory_lookup":
        return _summarize_inventory_lookup(result, arguments)
    if tool_name == "node_health_remediate":
        return _summarize_node_health(result, arguments)

    return _summarize_generic(tool_name, status, result, arguments)


def _summarize_error(
    tool_name: str,
    status: str,
    arguments: dict[str, Any],
    error: Any,
) -> str:
    if tool_name == "node_health_remediate" and isinstance(error, dict):
        return _summarize_node_health(error, arguments, status=status)

    lines = [
        f"Tool: {tool_name}",
        f"Status: {status}",
        f"Arguments: {_compact_json(arguments)}",
        f"Error: {_compact_text(error)}",
    ]
    return "\n".join(lines)


def _summarize_alarm_enrichment(result: Any, arguments: dict[str, Any]) -> str:
    data = _as_dict(result)
    alarms = _as_list(data.get("alarms"))
    error_info = _as_dict(data.get("error_info"))

    lines = [
        "Tool: noc_alarm_enrichment",
        "Status: success",
        f"Arguments: {_compact_json(arguments)}",
        f"Alarm type: {data.get('alarm_type') or arguments.get('alarm_type')}",
        f"Window minutes: {data.get('window_min') or arguments.get('window_min')}",
        f"Total alarms in DB: {data.get('total_alarms_in_db')}",
        f"Displayed alarms: {data.get('displayed_alarms') or len(alarms)}",
    ]
    if error_info:
        lines.append(
            "Error info: "
            f"code={error_info.get('error_code')}, "
            f"name={error_info.get('name')}, "
            f"domain={error_info.get('domain')}, "
            f"default_severity={error_info.get('default_severity')}, "
            f"description={error_info.get('description')}"
        )

    severities = _count_by(alarms, "severity")
    statuses = _count_by(alarms, "status")
    if severities:
        lines.append(f"Displayed severity counts: {_compact_json(severities)}")
    if statuses:
        lines.append(f"Displayed status counts: {_compact_json(statuses)}")

    frame_values: list[Any] = []
    for alarm in alarms:
        extracted = _as_dict(alarm.get("extracted"))
        frame = extracted.get("frame")
        if isinstance(frame, list):
            frame_values.extend(frame)
        elif frame:
            frame_values.append(frame)
    if "frame" in _compact_json(arguments).lower() or any(
        "frame" in _compact_json(_as_dict(alarm.get("extracted"))).lower()
        for alarm in alarms
    ):
        frame_summary = (
            f"matched values {frame_values[:10]}"
            if frame_values
            else "no frame values matched in displayed observations"
        )
        lines.append(f"Custom frame extraction: {frame_summary}")

    lines.append("Top displayed alarms:")
    for alarm in alarms[:8]:
        enrichment = _first_dict(_as_list(alarm.get("enrichment")))
        extracted = _as_dict(alarm.get("extracted"))
        lines.append(
            "- "
            f"alarm_id={alarm.get('alarm_id')}, "
            f"error={alarm.get('error_name')}, "
            f"device_id={alarm.get('device_id')}, "
            f"device_name={enrichment.get('device_name')}, "
            f"ip={enrichment.get('ip_address')}, "
            f"type={enrichment.get('device_type')}, "
            f"vendor={enrichment.get('vendor')}, "
            f"station={enrichment.get('station')}, "
            f"severity={alarm.get('severity')}, "
            f"status={alarm.get('status')}, "
            f"st={_as_dict(extracted.get('kv')).get('st')}, "
            f"time_created={alarm.get('time_created')}"
        )

    if data.get("note"):
        lines.append(f"Note: {data.get('note')}")
    return "\n".join(lines)


def _summarize_alarm_statistics(result: Any, arguments: dict[str, Any]) -> str:
    data = _as_dict(result)
    lines = [
        "Tool: alarm_statistics",
        "Status: success",
        f"Arguments: {_compact_json(arguments)}",
        f"Window minutes: {data.get('window_min')}",
        f"Group by: {data.get('group_by')}",
        f"Total alarms: {data.get('total_alarms')}",
        f"Active alarms: {data.get('active_alarms')}",
        f"Archived alarms: {data.get('archived_alarms')}",
        f"Severity filter: {data.get('severity_filter')}",
        f"Domain filter: {data.get('domain_filter')}",
        f"By severity: {_compact_json(data.get('by_severity'))}",
        f"Grouped alarms: {_compact_json(_as_list(data.get('grouped_alarms'))[:10])}",
        f"Top error codes: {_compact_json(_as_list(data.get('by_error_code'))[:10])}",
        f"Top devices: {_compact_json(_as_list(data.get('top_devices'))[:10])}",
    ]
    return "\n".join(lines)


def _summarize_inventory_lookup(result: Any, arguments: dict[str, Any]) -> str:
    data = _as_dict(result)
    device = _as_dict(data.get("device"))
    active_alarms = _as_list(data.get("active_alarms"))
    lines = [
        "Tool: device_inventory_lookup",
        "Status: success",
        f"Arguments: {_compact_json(arguments)}",
        "Device: "
        f"id={device.get('device_id')}, "
        f"name={device.get('name')}, "
        f"type={device.get('device_type')}, "
        f"ip={device.get('ip_address')}, "
        f"vendor={device.get('vendor')}, "
        f"station={device.get('station')}",
        f"Active alarm count: {data.get('active_alarm_count') or len(active_alarms)}",
    ]
    if active_alarms:
        lines.append("Active alarms:")
        for alarm in active_alarms[:10]:
            lines.append(
                "- "
                f"alarm_id={alarm.get('alarm_id')}, "
                f"error={alarm.get('error_name') or alarm.get('error_code')}, "
                f"severity={alarm.get('severity')}, "
                f"status={alarm.get('status')}, "
                f"time_created={alarm.get('time_created')}"
            )
    return "\n".join(lines)


def _summarize_node_health(
    result: Any,
    arguments: dict[str, Any],
    status: str = "success",
) -> str:
    data = _as_dict(result)
    lines = [
        "Tool: node_health_remediate",
        f"Status: {status}",
        f"Arguments: {_compact_json(arguments)}",
        f"Host: {data.get('host') or arguments.get('host')}",
        f"Service: {data.get('service') or arguments.get('service')}",
        f"Dry run: {data.get('dry_run')}",
        f"Action: {data.get('action')}",
        f"Executed: {data.get('executed')}",
        f"Command succeeded: {data.get('command_succeeded')}",
        f"Exit code: {data.get('exit_code')}",
        f"Approval status: {data.get('approval_status')}",
        f"Command: {data.get('command')}",
        f"Metrics: {_compact_json(data.get('metrics'))}",
        f"Thresholds: {_compact_json(data.get('thresholds'))}",
        f"Evaluation: {_compact_json(data.get('evaluation'))}",
        f"Reason: {data.get('reason')}",
        f"Stderr: {data.get('stderr')}",
    ]
    return "\n".join(line for line in lines if not line.endswith(": None"))


def _summarize_generic(
    tool_name: str,
    status: str,
    result: Any,
    arguments: dict[str, Any],
) -> str:
    return "\n".join(
        [
            f"Tool: {tool_name}",
            f"Status: {status}",
            f"Arguments: {_compact_json(arguments)}",
            f"Result: {_compact_json(result, max_len=3500)}",
        ]
    )


def _count_by(items: list[Any], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_dict(values: list[Any]) -> dict[str, Any]:
    return values[0] if values and isinstance(values[0], dict) else {}


def _compact_text(value: Any, max_len: int = 1000) -> str:
    text = str(value)
    return text if len(text) <= max_len else text[:max_len] + "...[truncated]"


def _compact_json(value: Any, max_len: int = 1500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= max_len else text[:max_len] + "...[truncated]"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Real):
        value = float(value)
        if math.isnan(value):
            return "nan"
        return value
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)
