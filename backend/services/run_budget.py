"""Conservative per-run admission accounting, separate from provider billing."""

import json
import math
from core.redaction import redact
from core.schemas import NormalizedMessage
from services.context_manager import prepare_context, estimate


class BudgetExceeded(ValueError):
    def __init__(self, code):
        self.code = code


class RunBudget:
    def __init__(self, state=None):
        from services.run_configuration import execution_settings
        self.config = execution_settings().model_copy(deep=True)
        self.tokens = int((state or {}).get("tokens", 0))
        self.cost = (state or {}).get("estimated_cost", 0.0)
        self.prices = json.loads(self.config.llm_model_prices_json)
        self.price_version = (state or {}).get(
            "price_config_version", self.config.llm_price_config_version
        )

    def reserve(self, provider, messages, tools):
        candidates = [provider, *getattr(provider, "fallbacks", [])]
        rates = [
            self.prices.get(getattr(candidate, "model_name", "unknown"))
            for candidate in candidates
        ]
        if self.config.llm_run_cost_limit is not None and (
            self.cost is None or self.price_version != self.config.llm_price_config_version
        ):
            raise BudgetExceeded("RUN_COST_HISTORY_UNAVAILABLE")
        if self.config.llm_run_cost_limit is not None and any(
            rate is None for rate in rates
        ):
            raise BudgetExceeded("MODEL_PRICE_UNCONFIGURED")
        self.rates_known = all(rate is not None for rate in rates)
        self.input_rate = max(
            (rate["input_per_million"] for rate in rates if rate), default=0
        )
        self.output_rate = max(
            (rate["output_per_million"] for rate in rates if rate), default=0
        )
        self.attempt_bound = self.config.llm_max_attempts * len(candidates)
        safe = [
            NormalizedMessage.model_validate(redact(message)) for message in messages
        ]
        context = prepare_context(
            safe,
            tools,
            self.config.llm_context_tokens,
            self.config.llm_reserved_output_tokens,
            self.config.llm_context_margin,
            self.config.context_summary_enabled,
        )
        self.input_estimate = estimate(context, tools)
        projection = (
            self.input_estimate + self.config.llm_output_max_bytes
        ) * self.attempt_bound
        if self.tokens + projection > self.config.llm_run_token_limit:
            raise BudgetExceeded("RUN_TOKEN_BUDGET_EXCEEDED")
        estimated_cost = (
            (
                self.input_estimate * self.input_rate
                + self.config.llm_output_max_bytes * self.output_rate
            )
            / 1_000_000
            * self.attempt_bound
        )
        if not math.isfinite(estimated_cost):
            raise BudgetExceeded("INVALID_MODEL_PRICE")
        if (
            self.config.llm_run_cost_limit is not None
            and (self.cost or 0) + estimated_cost > self.config.llm_run_cost_limit
        ):
            raise BudgetExceeded("RUN_COST_BUDGET_EXCEEDED")

    def record(self, usage, text, thinking, calls):
        reported = usage and all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in ("input", "output")
        )
        input_tokens = usage["input"] if reported else self.input_estimate
        output_tokens = (
            usage["output"]
            if reported
            else len(
                (
                    text
                    + thinking
                    + json.dumps(
                        [call.model_dump() for call in calls], ensure_ascii=False
                    )
                ).encode()
            )
        )
        self.tokens += (input_tokens + output_tokens) * self.attempt_bound
        if self.rates_known and self.cost is not None:
            self.cost += (
                (input_tokens * self.input_rate + output_tokens * self.output_rate)
                / 1_000_000
                * self.attempt_bound
            )
        else:
            self.cost = None

    def snapshot(self):
        return {
            "tokens": self.tokens,
            "estimated_cost": self.cost,
            "currency": self.config.llm_budget_currency,
            "price_config_version": self.config.llm_price_config_version,
            "accounting": "conservative admission estimate, not provider billing",
        }
