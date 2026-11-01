"""External worker entry point: python -m worker (from backend)."""
import asyncio
import signal
from contextlib import suppress
from repositories.engine import async_session, init_db
from services.runtime import create_runtime, close_runtime
from services.run_worker import RunWorker
from controllers.chat import _get_provider


async def main():
    await init_db()
    engine, resources = await create_runtime()
    worker = RunWorker(engine, async_session, _get_provider)
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(event, worker.stop.set)
    try:
        task = asyncio.create_task(worker.run_forever())
        stopping = asyncio.create_task(worker.stop.wait())
        try:
            done, _ = await asyncio.wait([task, stopping], return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(task, timeout=30)
        finally:
            stopping.cancel()
            with suppress(asyncio.CancelledError): await stopping
    finally:
        await close_runtime(resources)


if __name__ == "__main__":
    asyncio.run(main())
