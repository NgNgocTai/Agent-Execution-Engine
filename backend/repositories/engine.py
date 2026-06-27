from sqlalchemy import inspect, text
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
    from repositories.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_message_columns)


def _ensure_message_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    if "messages" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("messages")}
    if "thinking_content" not in existing_columns:
        sync_conn.execute(
            text("ALTER TABLE messages ADD COLUMN thinking_content TEXT")
        )
