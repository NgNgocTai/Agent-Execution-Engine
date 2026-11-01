"""Release per-run HTTP clients without overriding a completed run outcome."""
import asyncio
import inspect
import logging

logger = logging.getLogger(__name__)


async def close_provider(provider):
    for candidate in [provider, *getattr(provider, "fallbacks", [])]:
        client = getattr(candidate, "client", None)
        close = getattr(client, "close", None)
        if close:
            try:
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, 2)
            except Exception:
                logger.warning("Provider client cleanup failed")
