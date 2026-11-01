from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    from repositories.migrations import upgrade
    async with engine.begin() as conn:
        if settings.deployment_mode == "production":
            from sqlalchemy import text
            from alembic.script import ScriptDirectory
            from repositories.migrations import configuration
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != ScriptDirectory.from_config(configuration()).get_current_head():
                raise RuntimeError("Run the release migration before starting the application")
        else:
            await conn.run_sync(upgrade)
