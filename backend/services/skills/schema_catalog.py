"""Allow-listed schema discovery: column metadata only, never rows or free SQL.

Each source is described independently, so one unavailable database does not
hide the other's schema. Tables outside the configured allowlist are refused,
and tables that are allow-listed but absent are reported as missing.
"""

import asyncio
from typing import Literal

from pydantic import Field

from core.config import settings
from services.connectors import ConnectorUnavailable
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.input_models import SkillInput

NOTE = "Metadata only: no row data was read and no query text was accepted."


def allowlist():
    def names(value):
        return [name for name in value.split(",") if name]

    return {
        "alarms": names(settings.schema_discovery_clickhouse_tables),
        "inventory": names(settings.schema_discovery_postgres_tables),
    }


class SchemaInput(SkillInput):
    source: Literal["all", "alarms", "inventory"] = "all"
    table: str = Field(default="", max_length=63, pattern=r"^(?:[a-z_][a-z0-9_]*)?$")


class SchemaCatalogSkill(BaseSkill):
    input_model = SchemaInput

    def __init__(self, hub) -> None:
        self.hub = hub

    @property
    def name(self) -> str:
        return "schema_catalog"

    @property
    def description(self) -> str:
        return (
            "Describe the columns of allow-listed alarm (ClickHouse) and inventory "
            "(PostgreSQL) tables: names and types only, never rows. Use it to "
            "understand fields before interpreting tool results."
        )

    @property
    def parameters_schema(self) -> dict:
        return SchemaInput.model_json_schema()

    async def _describe(self, source, tables):
        try:
            if source == "alarms":
                columns = await asyncio.to_thread(self.hub.alarms.describe_tables, tables)
            else:
                columns = await self.hub.devices.describe_tables(tables)
        except ConnectorUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}
        grouped = {}
        for column in columns:
            entry = {key: value for key, value in column.items() if key != "table"}
            grouped.setdefault(column["table"], []).append(entry)
        return {
            "status": "ok",
            "tables": grouped,
            "missing_tables": [table for table in tables if table not in grouped],
        }

    async def execute(self, source: str = "all", table: str = "", **kwargs) -> SkillResult:
        allowed = allowlist()
        sources = ["alarms", "inventory"] if source == "all" else [source]
        if table:
            owners = [name for name in sources if table in allowed[name]]
            if not owners:
                return SkillResult(
                    status=SkillStatus.BUSINESS_ERROR,
                    data={"error_code": "TABLE_NOT_ALLOWED", "allowed": {s: allowed[s] for s in sources}},
                    error="That table is not in the schema discovery allowlist",
                )
            sources = owners
        selected = {name: ([table] if table else allowed[name]) for name in sources}
        results = await asyncio.gather(
            *(self._describe(name, tables) for name, tables in selected.items() if tables)
        )
        described = dict(zip([name for name, tables in selected.items() if tables], results))
        for name, tables in selected.items():
            if not tables:
                described[name] = {"status": "not_configured", "tables": {}, "missing_tables": []}
        if all(entry["status"] == "unavailable" for entry in described.values()):
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data={"error_code": "CONNECTOR_UNAVAILABLE", "sources": described},
                error="No schema source is available; no substitute schema was used.",
            )
        return SkillResult(status=SkillStatus.SUCCESS, data={"sources": described, "note": NOTE})
