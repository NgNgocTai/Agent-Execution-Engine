from contextvars import ContextVar
from uuid import uuid4
import hashlib

request_id = ContextVar("request_id", default=None)
run_id = ContextVar("run_id", default=None)


def step_identifier(run, key):
    return hashlib.sha256((run + ":" + key).encode()).hexdigest()[:32] if run else None


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identifier = uuid4().hex
        context = request_id.set(identifier)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", identifier.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id.reset(context)
