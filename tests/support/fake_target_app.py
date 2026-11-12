"""ASGI wrapper so the test-only FakeSystem can run as a real local process for tester self-checks.

This is not the Team 9 system and must never be deployed or registered as a target.
"""

import os

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from tests.support.fake_phase4 import FakePhase4
from tests.support.fake_phase5 import FakePhase5
from tests.support.fake_system import FakeSystem

# FAKE_TARGET_PHASE=4/5 serves the test-only Phase 4/5 fake; the default stays Phase 3.
_PHASE = os.environ.get("FAKE_TARGET_PHASE")
# Over real sockets the tester sleeps in real time between polls, so the Phase 5 live
# self-check finishes accepted work on the first poll (busy-window cases then skip as
# unobservable; the in-process unit tests exercise them with a virtual clock).
fake: FakeSystem | FakePhase4 = (
    FakePhase5(processing_polls=0, remediation_polls=0, large_polls=0)
    if _PHASE == "5"
    else FakePhase4()
    if _PHASE == "4"
    else FakeSystem()
)


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    query = scope["query_string"].decode()
    url = f"http://127.0.0.1:8000{scope['path']}" + (f"?{query}" if query else "")
    headers = [(key.decode(), value.decode()) for key, value in scope["headers"]]
    request = httpx.Request(scope["method"], url, headers=headers, content=body)
    try:
        response = await run_in_threadpool(fake.handle, request)
    except httpx.TransportError:
        return  # close without a response, like a dropped connection
    await send(
        {
            "type": "http.response.start",
            "status": response.status_code,
            "headers": [(k.encode(), v.encode()) for k, v in response.headers.items()],
        }
    )
    await send({"type": "http.response.body", "body": response.content})
