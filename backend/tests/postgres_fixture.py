from contextlib import asynccontextmanager
import os
from uuid import uuid4
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema


@asynccontextmanager
async def postgres_database():
    url = make_url(os.environ["APPROVAL_TEST_POSTGRES_URL"])
    if url.drivername != "postgresql+asyncpg" or url.database != "noc_agent_approval_test" or url.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Use the disposable local noc_agent_approval_test database")
    schema = "upgrade_test_" + uuid4().hex
    admin = create_async_engine(url)
    isolated = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with admin.begin() as connection: await connection.execute(CreateSchema(schema))
        yield isolated
    finally:
        await isolated.dispose()
        async with admin.begin() as connection: await connection.execute(DropSchema(schema, cascade=True))
        await admin.dispose()
