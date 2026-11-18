import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from controllers.chat import router as chat_router, set_engine
from core.config import settings
from core.redaction import RedactingFilter
from core.auth import AuthenticationMiddleware
from core.request_context import RequestContextMiddleware
from repositories.clickhouse_repo import AlarmRepository
from repositories.engine import init_db
from repositories.postgres_repo import DeviceRepository
from services.agent_service import AgentEngine
from services.skills.noc_alarm_enrichment import NocAlarmEnrichmentSkill
from services.skills.node_health_remediate import NodeHealthRemediateSkill
from services.skills.alarm_statistics import AlarmStatisticsSkill
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor
from tracing.langfuse_tracer import LangfuseTracer

logging.basicConfig(level=logging.INFO)
for handler in logging.getLogger().handlers:
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s request_id=%(request_id)s run_id=%(run_id)s %(message)s"))
logger = logging.getLogger(__name__)

app = FastAPI(title="NOC Agent API", version="1.0.0")
app.add_middleware(AuthenticationMiddleware)
app.add_middleware(RequestContextMiddleware)
cors_origins = [
    origin.strip()
    for origin in settings.cors_origins.split(",")
    if origin.strip()
] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("Control database schema is ready")

    from services.runtime import create_runtime
    from services.run_worker import RunWorker
    from repositories.engine import async_session
    from controllers.chat import _get_provider
    engine, resources = await create_runtime()
    app.state.resources = resources
    from services.connectors import ConnectorHub

    app.state.hub = next((r for r in resources if isinstance(r, ConnectorHub)), None)
    set_engine(engine)
    app.state.worker = None
    app.state.worker_task = None
    if settings.worker_mode == "embedded":
        worker = RunWorker(engine, async_session, _get_provider)
        app.state.worker = worker
        app.state.worker_task = asyncio.create_task(worker.run_forever())
    app.state.ready = True


@app.on_event("shutdown")
async def shutdown():
    from services.runtime import close_runtime
    app.state.ready = False
    if app.state.worker:
        app.state.worker.stop.set()
        try:
            await asyncio.wait_for(app.state.worker_task, timeout=30)
        except TimeoutError:
            pass
    await close_runtime(app.state.resources)


app.include_router(chat_router)
from controllers.runs import router as runs_router
app.include_router(runs_router)
from controllers.skills import router as skills_router
app.include_router(skills_router)
from controllers.operations import router as operations_router
app.include_router(operations_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    from fastapi import HTTPException
    if not getattr(app.state, "ready", False):
        raise HTTPException(503, "Application is starting or draining")
    if app.state.worker_task is not None and app.state.worker_task.done():
        raise HTTPException(503, "Embedded worker stopped")
    try:
        from repositories.engine import engine as control_engine
        from sqlalchemy import text
        async with asyncio.timeout(2):
            async with control_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "Control database is unavailable") from None
    # Data sources never gate readiness (the control database does); their
    # measured state is reported so an outage is visible, not hidden.
    hub = getattr(app.state, "hub", None)
    sources = await hub.probe() if hub else {}
    degraded = any(s["state"] == "unavailable" for s in sources.values())
    return {"status": "degraded" if degraded else "ready", "sources": sources}
