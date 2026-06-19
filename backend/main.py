import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from controllers.chat import router as chat_router, set_engine
from repositories.clickhouse_repo import AlarmRepository
from repositories.engine import init_db
from repositories.postgres_repo import DeviceRepository
from services.agent_service import AgentEngine
from services.skills.noc_alarm_enrichment import NocAlarmEnrichmentSkill
from services.skills.node_health_remediate import NodeHealthRemediateSkill
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor
from tracing.langfuse_tracer import LangfuseTracer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="NOC Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("SQLite initialized")

    alarm_repo = AlarmRepository()
    logger.info("ClickHouse connected")

    device_repo = DeviceRepository()
    await device_repo.connect()
    logger.info("PostgreSQL connected")

    registry = SkillRegistry()
    registry.register(NocAlarmEnrichmentSkill(alarm_repo, device_repo))
    registry.register(NodeHealthRemediateSkill())

    tool_executor = ToolExecutor(registry)

    tracer = LangfuseTracer()

    engine = AgentEngine(
        tool_executor=tool_executor,
        skill_registry=registry,
        tracer=tracer,
    )
    set_engine(engine)

    logger.info("Skills registered: %s", registry.list_names())
    logger.info("NOC Agent API ready")


app.include_router(chat_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
