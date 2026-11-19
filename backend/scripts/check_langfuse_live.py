from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from langfuse import Langfuse, propagate_attributes

from core.config import settings


def _clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _find_observations(host: str, public_key: str, secret_key: str, trace_id: str, since: datetime) -> tuple[int, list | str]:
    """Read back through the v2 observations API (the v1 trace endpoint was removed)."""
    until = datetime.now(timezone.utc) + timedelta(minutes=1)
    query = urlencode({
        "fields": "core,basic",
        "fromStartTime": since.isoformat().replace("+00:00", "Z"),
        "toStartTime": until.isoformat().replace("+00:00", "Z"),
        "limit": 100,
    })
    req = Request(
        f"{host.rstrip('/')}/api/public/v2/observations?{query}",
        headers={"Authorization": _auth_header(public_key, secret_key), "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode("utf-8")).get("data", [])
            return resp.status, [row for row in rows if row.get("traceId") == trace_id]
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"Không kết nối được Langfuse API: {exc}") from exc


def run_live_check(wait_seconds: int, poll_interval: float) -> int:
    public_key = _clean_env_value(settings.langfuse_public_key)
    secret_key = _clean_env_value(settings.langfuse_secret_key)
    host = _clean_env_value(settings.langfuse_host)

    if not public_key or not secret_key:
        print("FAIL: thiếu LANGFUSE_PUBLIC_KEY hoặc LANGFUSE_SECRET_KEY.")
        return 2

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id = f"live-check-session-{uuid4().hex}"
    client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    trace_id = client.create_trace_id()

    print("Creating Langfuse live-check trace...")
    print(f"Host: {host}")
    print(f"Trace ID: {trace_id}")
    print(f"Session ID: {session_id}")

    with propagate_attributes(session_id=session_id, trace_name="langfuse-live-check"):
        root = client.start_observation(
            trace_context={"trace_id": trace_id},
            name="langfuse-live-check",
            as_type="agent",
            input={"message": "Langfuse live check from backend script"},
            metadata={"source": "backend/scripts/check_langfuse_live.py"},
        )
        generation = root.start_observation(
            name="live-check-generation", as_type="generation", model="live-check",
            input=[{"role": "user", "content": "ping"}],
        )
        generation.update(output="pong", usage_details={"input": 1, "output": 1})
        generation.end()
        tool = root.start_observation(name="tool-live-check", as_type="tool", input={"tool": "noop"})
        tool.update(output={"status": "success"})
        tool.end()
    root.update(output="Langfuse live check completed")
    root.end()
    client.flush()

    deadline = time.monotonic() + wait_seconds
    last_status, last_body = None, ""
    while time.monotonic() <= deadline:
        status, found = _find_observations(host, public_key, secret_key, trace_id, since)
        last_status, last_body = status, found
        if status in (401, 403):
            print("FAIL: Langfuse API từ chối credentials (401/403).")
            return 3
        if status != 200:
            print(f"FAIL: Langfuse API trả status {status}.")
            print(str(found)[:500])
            return 4
        names = sorted(row.get("name") for row in found)
        if len(found) >= 3:
            print("PASS: trace đã được ghi và đọc lại qua API v2.")
            print(f"Observations: {names}")
            return 0
        time.sleep(poll_interval)

    print("FAIL: đã flush nhưng chưa đọc lại đủ observation trước timeout.")
    print(f"Last status: {last_status}; found: {str(last_body)[:300]}")
    return 5


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and verify a Langfuse live-check trace.")
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    return run_live_check(args.wait_seconds, args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
