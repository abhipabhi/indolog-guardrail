# COPIED FROM libs/guardrail/testbench/bench/guardrail_io.py (branch feat/guardrail-testbench) BY sync_guardrail_flow.py. DO NOT EDIT HERE.
#
# Not covered by the LICENSE in this folder: this file belongs to the Logistics-AI-Platform
# repository. tests/test_copied_flow.py fails when it is not exactly its source; to take a
# change, run `python sync_guardrail_flow.py`.
"""Runs the backend's gateway functions, and keeps the guardrail's answers for the tester to see.

The gateway (bench/gateway.py, the backend's own module) hands back verdicts: a status, reason
codes, findings. That is all the backend needs. A tester also needs which checks ran, the request
ids, and the raw response. The gateway is not edited to give them up: it has a transport hook for
its HTTP client (`_transport`, which its own tests use), and a transport that remembers what went
by is put there.

Each test run records into its own list, found through a context variable, so two testers running
tests at the same moment do not see each other's calls.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

import httpx

from . import gateway

T = TypeVar("T")


@dataclass(frozen=True)
class Exchange:
    """One request to the guardrail and what came back."""

    path: str                       # /guardrail/input, /guardrail/output or /guardrail/answer
    request: dict
    http_status: int | None         # None: no answer at all (unreachable, timed out)
    body: dict | None               # the JSON answer, when there was one


_recording: contextvars.ContextVar[list[Exchange] | None] = contextvars.ContextVar("testbench_recording", default=None)

# How the real connection is made. Tests point this at the guardrail's own ASGI app.
connect: Callable[[], httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport


class _Remembering(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sink = _recording.get()
        try:
            sent = json.loads(request.content or b"{}")
        except ValueError:
            sent = {}
        inner = connect()
        try:
            try:
                response = await inner.handle_async_request(request)
                content = await response.aread()
                await response.aclose()
            except Exception:
                if sink is not None:
                    sink.append(Exchange(request.url.path, sent, None, None))
                raise
        finally:
            await inner.aclose()
        try:
            answered = json.loads(content)
        except ValueError:
            answered = None
        if sink is not None:
            sink.append(Exchange(request.url.path, sent, response.status_code, answered if isinstance(answered, dict) else None))
        return httpx.Response(response.status_code, content=content, request=request,
                              headers={"content-type": response.headers.get("content-type", "application/json")})


gateway._transport = _Remembering()


def call(make: Callable[[], Awaitable[T]]) -> tuple[T, list[Exchange]]:
    """Runs one gateway call (or several, in one coroutine) and returns its result with every
    exchange it made with the guardrail."""
    seen: list[Exchange] = []

    async def run() -> T:
        _recording.set(seen)
        return await make()

    return asyncio.run(run()), seen


def get_json(path: str) -> dict | None:
    """GET /health or /rules. None when the guardrail gives no usable answer."""
    base = (gateway.settings.guardrail_url or "").strip().rstrip("/")
    if not base:
        return None

    async def fetch() -> dict | None:
        inner = connect()
        try:
            async with httpx.AsyncClient(timeout=max(gateway.settings.guardrail_timeout_ms, 1) / 1000.0, transport=inner) as client:
                response = await client.get(f"{base}{path}")
            body = response.json() if response.status_code == 200 else None
            return body if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    return asyncio.run(fetch())
