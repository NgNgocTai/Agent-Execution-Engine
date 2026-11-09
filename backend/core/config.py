import os
import json
import math
from typing import Literal
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "openai/gpt-oss-120b:free"
    openai_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_api_key: str = ""
    anthropic_model: str = "anthropic/claude-3-haiku"
    anthropic_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_protocol: Literal["auto", "anthropic", "openai_compatible"] = "auto"
    anthropic_max_tokens: int = 4096
    anthropic_reasoning_max_tokens: int = 1024
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek/deepseek-r1"
    deepseek_base_url: str = "https://openrouter.ai/api/v1"
    database_url: str = "sqlite+aiosqlite:///./conversations.db"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_trace_thinking_content: bool = False
    llm_timeout_seconds: float = Field(default=90, gt=0, le=600)
    llm_max_attempts: int = Field(default=2, ge=1, le=3)
    llm_context_tokens: int = Field(default=32768, ge=2048, le=1000000)
    llm_reserved_output_tokens: int = Field(default=4096, ge=256, le=32000)
    llm_context_margin: int = Field(default=1024, ge=256, le=16000)
    context_summary_enabled: bool = True
    llm_run_token_limit: int = Field(default=1000000, ge=1024, le=100000000)
    llm_run_cost_limit: float | None = Field(default=None, ge=0)
    llm_model_prices_json: str = "{}"
    llm_budget_currency: str = "USD"
    llm_price_config_version: str = "unconfigured"
    llm_fallback_providers: str = ""
    llm_output_max_bytes: int = Field(default=65536, ge=1024, le=1048576)
    llm_tool_batch_limit: int = Field(default=32, ge=1, le=100)
    run_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    worker_mode: Literal["embedded", "external"] = "embedded"
    data_mode: Literal["live", "fixture"] = "live"
    sandbox_image: str = ""
    skill_validation_timeout_seconds: float = Field(default=120, ge=5, le=300)
    builtin_skills_directory: str = ""
    max_active_runs_per_actor: int = Field(default=4, ge=1, le=100)
    fixture_delay_seconds: float = Field(default=0, ge=0, le=10)
    tool_output_max_bytes: int = Field(default=16384, ge=1024, le=65536)
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_known_hosts: str = ""
    ssh_auto_add_host_keys: bool = False
    ssh_allowed_hosts: str = ""
    ssh_allowed_services: str = ""
    allow_docker_restart: bool = False
    remediation_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    approval_action_timeout_seconds: float = Field(default=45, gt=0, le=300)
    ssh_connect_timeout_seconds: float = Field(default=5, gt=0, le=120)
    ssh_auth_timeout_seconds: float = Field(default=5, gt=0, le=120)
    ssh_total_timeout_seconds: float = Field(default=20, gt=0, le=300)
    ssh_output_max_bytes: int = Field(default=65536, ge=1, le=1048576)
    clickhouse_host: str = ""
    clickhouse_port: int = 8123
    clickhouse_user: str = ""
    clickhouse_password: str = ""
    clickhouse_database: str = "alarm_data"
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""
    cors_origins: str = "http://localhost:5173"
    auth_mode: Literal["local", "bearer"] = "local"
    auth_principals_json: str = "[]"
    deployment_mode: Literal["development", "production"] = "development"

    @model_validator(mode="after")
    def validate_security(self):
        prices = json.loads(self.llm_model_prices_json)
        if not isinstance(prices, dict):
            raise ValueError("Model prices must be a JSON object")
        for price in prices.values():
            if not isinstance(price, dict) or set(price) != {
                "input_per_million",
                "output_per_million",
            }:
                raise ValueError(
                    "Each model price needs input_per_million/output_per_million"
                )
            if any(
                type(value) not in {float, int} or not math.isfinite(value) or value < 0
                for value in price.values()
            ):
                raise ValueError("Model prices must be non-negative numbers")
        if (
            self.llm_reserved_output_tokens + self.llm_context_margin
            >= self.llm_context_tokens
        ):
            raise ValueError(
                "LLM context must leave space for input after output reservation and margin"
            )
        if any(
            name.strip() not in {"openai", "anthropic", "deepseek"}
            for name in self.llm_fallback_providers.split(",")
            if name.strip()
        ):
            raise ValueError("Unknown fallback provider")
        entries = json.loads(self.auth_principals_json)
        if not isinstance(entries, list):
            raise ValueError("AUTH_PRINCIPALS_JSON must be a list")
        subjects, digests = set(), set()
        roles = {"viewer", "operator", "approver", "skill_reviewer", "admin"}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {
                "subject",
                "token_sha256",
                "roles",
                "hosts",
                "services",
            }:
                raise ValueError("Invalid auth principal fields")
            if not isinstance(entry.get("roles"), list) or any(
                not isinstance(value, str) for value in entry["roles"]
            ):
                raise ValueError("Principal roles must be a list")
            if any(
                not isinstance(entry.get(name, []), list)
                or any(not isinstance(value, str) for value in entry.get(name, []))
                for name in ("hosts", "services")
            ):
                raise ValueError("Principal scopes must be lists of strings")
            digest = entry.get("token_sha256", "")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("Auth token must be a SHA-256 hex digest")
            subject = entry.get("subject")
            if (
                not isinstance(subject, str)
                or not 1 <= len(subject) <= 255
                or subject != subject.strip()
                or not set(entry.get("roles", [])).issubset(roles)
            ):
                raise ValueError("Invalid auth principal")
            if entry["subject"] in subjects or digest in digests:
                raise ValueError("Duplicate auth principal or token")
            subjects.add(entry["subject"])
            digests.add(digest)
        if self.auth_mode == "bearer" and not entries:
            raise ValueError("Bearer authentication requires principals")
        if self.deployment_mode == "production":
            origins = {
                value.strip() for value in self.cors_origins.split(",") if value.strip()
            }
            if self.auth_mode != "bearer" or not origins or "*" in origins:
                raise ValueError(
                    "Production requires bearer auth and explicit CORS origins"
                )
            if not any("admin" in entry["roles"] for entry in entries):
                raise ValueError("Production requires an administration principal")
            if (
                not self.database_url.startswith("postgresql+asyncpg://")
                or "local-development-only" in self.database_url
            ):
                raise ValueError(
                    "Production requires a configured PostgreSQL control database"
                )
            if self.ssh_auto_add_host_keys:
                raise ValueError("Production forbids automatic SSH host key acceptance")
            if self.worker_mode != "external" or self.data_mode != "live":
                raise ValueError(
                    "Production requires an external worker and live data sources"
                )
        return self

    @property
    def resolved_anthropic_protocol(self):
        if self.anthropic_protocol != "auto":
            return self.anthropic_protocol
        from urllib.parse import urlsplit
        host = urlsplit(self.anthropic_base_url).hostname
        return "anthropic" if not self.anthropic_base_url or host == "api.anthropic.com" else "openai_compatible"

    model_config = {
        "env_file": os.environ.get("NOC_AGENT_ENV_FILE", str(ROOT_DIR / ".env")),
        "env_file_encoding": "utf-8",
        # The root dotenv is also shared with the optional evaluation scripts.
        # Their settings must not prevent the backend from starting.
        "extra": "ignore",
    }


settings = Settings()
