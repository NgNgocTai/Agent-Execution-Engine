from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from langfuse import Langfuse

from core.config import settings


def _clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _get_trace(host: str, public_key: str, secret_key: str, trace_id: str) -> tuple[int, str]:
    url = f"{host.rstrip('/')}/api/public/traces/{trace_id}"
    req = Request(
        url,
        headers={
            "Authorization": _auth_header(public_key, secret_key),
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except URLError as exc:
        raise RuntimeError(f"Không kết nối được Langfuse API: {exc}") from exc


def run_live_check(wait_seconds: int, poll_interval: float) -> int:
    public_key = _clean_env_value(settings.langfuse_public_key)
    secret_key = _clean_env_value(settings.langfuse_secret_key)
    host = _clean_env_value(settings.langfuse_host)

    if not public_key or not secret_key:
        print("FAIL: thiếu LANGFUSE_PUBLIC_KEY hoặc LANGFUSE_SECRET_KEY.")
        return 2

    trace_id = f"codex-live-check-{uuid4().hex}"
    session_id = f"codex-live-check-session-{uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()

    print("Creating Langfuse live-check trace...")
    print(f"Host: {host}")
    print(f"Trace ID: {trace_id}")
    print(f"Session ID: {session_id}")

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
    )

    trace = client.trace(
        id=trace_id,
        name="codex-langfuse-live-check",
        session_id=session_id,
        input={"message": "Langfuse live check from backend script"},
        metadata={
            "source": "backend/scripts/check_langfuse_live.py",
            "created_at": now,
        },
    )
    generation = trace.generation(
        name="live-check-generation",
        model="codex-live-check",
        input=[{"role": "user", "content": "ping"}],
        metadata={"live_check": True},
    )
    generation.end(
        output="pong",
        usage_details={"input": 1, "output": 1},
        metadata={"status": "ok"},
    )
    span = trace.span(
        name="tool-live-check",
        input={"tool": "noop"},
        metadata={"live_check": True},
    )
    span.end(output={"status": "success", "result": "noop"})
    trace.update(
        output="Langfuse live check completed",
        metadata={"live_check_completed": True},
    )
    client.flush()

    deadline = time.monotonic() + wait_seconds
    last_status = None
    last_body = ""

    while time.monotonic() <= deadline:
        status, body = _get_trace(host, public_key, secret_key, trace_id)
        last_status = status
        last_body = body

        if status == 200:
            payload = json.loads(body)
            print("PASS: Langfuse trace đã được tạo và đọc lại qua API.")
            print(f"Trace name: {payload.get('name')}")
            print(f"Verified trace ID: {trace_id}")
            return 0

        if status in (401, 403):
            print("FAIL: Langfuse API từ chối credentials (401/403).")
            return 3

        if status != 404:
            print(f"FAIL: Langfuse API trả status {status}.")
            print(body[:500])
            return 4

        time.sleep(poll_interval)

    print("FAIL: đã flush nhưng chưa đọc lại được trace trước timeout.")
    print(f"Last status: {last_status}")
    print(last_body[:500])
    return 5


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and verify a Langfuse live-check trace.")
    parser.add_argument("--wait-seconds", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    return run_live_check(args.wait_seconds, args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
