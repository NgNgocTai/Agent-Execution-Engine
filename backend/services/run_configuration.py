"""Versioned, secret-free execution settings. Auth and infrastructure policy stay live."""

from contextvars import ContextVar
import hashlib
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from core.config import settings, Settings
from services.workflow.state import CheckpointIncompatible

FIELDS = (
    "openai_model",
    "openai_base_url",
    "anthropic_model",
    "anthropic_base_url",
    "anthropic_protocol",
    "anthropic_max_tokens",
    "anthropic_reasoning_max_tokens",
    "deepseek_model",
    "deepseek_base_url",
    "llm_fallback_providers",
    "llm_timeout_seconds",
    "llm_max_attempts",
    "llm_context_tokens",
    "llm_reserved_output_tokens",
    "llm_context_margin",
    "context_summary_enabled",
    "llm_run_token_limit",
    "llm_run_cost_limit",
    "llm_model_prices_json",
    "llm_budget_currency",
    "llm_price_config_version",
    "llm_output_max_bytes",
    "llm_tool_batch_limit",
)


OPTION_TYPES = {
    name: TypeAdapter(Settings.model_fields[name].rebuild_annotation())
    for name in FIELDS
}


class RunConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = 1
    provider: str
    prompt: str = Field(min_length=1, max_length=32768)
    prompt_hash: str
    options: dict

    @model_validator(mode="after")
    def validate_snapshot(self):
        if self.version != 1 or self.provider not in {
            "mock",
            "openai",
            "anthropic",
            "deepseek",
        }:
            raise ValueError("Unsupported run configuration")
        if set(self.options) != set(FIELDS):
            raise ValueError("Incomplete or unknown execution options")
        if self.prompt_hash != hashlib.sha256(self.prompt.encode()).hexdigest():
            raise ValueError("Prompt hash mismatch")
        if self.options["anthropic_protocol"] == "auto":
            raise ValueError("Protocol must be resolved before execution")
        for name, value in self.options.items():
            OPTION_TYPES[name].validate_python(value, strict=True)
        # Validate values using the same constraints as process configuration.
        Settings.model_validate({**settings.model_dump(), **self.options}, strict=True)
        for name in ("openai_base_url", "anthropic_base_url", "deepseek_base_url"):
            url = urlsplit(self.options[name])
            if url.username or url.password or url.query or url.fragment:
                raise ValueError(
                    "Provider URLs cannot embed credentials or query parameters"
                )
        return self


_configuration = ContextVar("run_configuration", default=None)


def execution_settings():
    config = _configuration.get()
    return settings.model_copy(update=config.options) if config else settings


def execution_prompt(default):
    config = _configuration.get()
    return config.prompt if config else default


def capture_configuration(provider):
    from services.prompts import SYSTEM_PROMPT

    options = {key: getattr(settings, key) for key in FIELDS}
    options["anthropic_protocol"] = settings.resolved_anthropic_protocol
    prompt = SYSTEM_PROMPT.strip()
    return RunConfiguration(
        provider=provider,
        prompt=prompt,
        prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
        options=options,
    ).model_dump_json()


def load_configuration(raw, provider):
    try:
        config = RunConfiguration.model_validate_json(raw) if raw else None
        if config is None or config.provider != provider:
            raise ValueError("Legacy or mismatched configuration")
    except ValueError as exc:
        raise CheckpointIncompatible(
            "Run configuration is missing or invalid; start a new run"
        ) from exc
    return config


def bind_configuration(raw, provider):
    return _configuration.set(load_configuration(raw, provider))


def configuration_summary(raw):
    if not raw:
        return {"version": None, "requires_new_run": True}
    try:
        config = RunConfiguration.model_validate_json(raw)
    except ValueError:
        return {"version": None, "requires_new_run": True}
    candidates = [
        config.provider,
        *[
            item.strip()
            for item in config.options["llm_fallback_providers"].split(",")
            if item.strip() not in {"", config.provider, "mock"}
        ],
    ]
    return {
        "version": config.version,
        "prompt_hash": config.prompt_hash,
        "candidates": [
            {
                "provider": name,
                "model": config.options.get(name + "_model", "mock"),
                "protocol": config.options["anthropic_protocol"]
                if name == "anthropic"
                else "openai_compatible"
                if name != "mock"
                else "mock",
            }
            for name in candidates
            if name in {"mock", "openai", "anthropic", "deepseek"}
        ],
    }
