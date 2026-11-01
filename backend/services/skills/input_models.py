"""Strict input contracts shared by tool discovery and execution."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NodeHealthInput(SkillInput):
    host: Name
    ram_threshold: int = Field(default=80, ge=0, le=100)
    cpu_threshold: int = Field(default=80, ge=0, le=100)
    service: str = Field(default="", max_length=128, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
    log_lines: int = Field(default=200, ge=1, le=1000)
    dry_run: bool = True


class AlarmStatisticsInput(SkillInput):
    window_min: int = Field(default=1440, ge=1, le=43200)
    domain: Literal["", "RAN", "Transport", "Power", "Core"] = ""
    severity: Literal["", "Critical", "Major", "Warning", "Minor"] = ""
    group_by: Literal["severity", "error_code", "device_id"] = "severity"
    top_n: int = Field(default=10, ge=1, le=100)


class InventoryInput(SkillInput):
    device_name: str = Field(default="", max_length=255)
    ip_address: str = Field(default="", max_length=64)
    device_id: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def one_identifier(self):
        if sum(bool(value.strip()) for value in (self.device_name, self.ip_address, self.device_id)) != 1:
            raise ValueError("Provide exactly one device identifier")
        return self


class AlarmEnrichmentInput(SkillInput):
    alarm_type: str = Field(min_length=1, max_length=128)
    window_min: int = Field(default=10, ge=1, le=43200)
    custom_patterns: dict = Field(default_factory=dict, max_length=0)

    @model_validator(mode="after")
    def no_untrusted_regex(self):
        # Python re has no deadline; user regex must not run on the web worker.
        if self.custom_patterns:
            raise ValueError("Custom regex requires a reviewed skill version")
        if not self.alarm_type.strip():
            raise ValueError("alarm_type must not be blank")
        return self
