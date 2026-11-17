"""Shared pieces for the tests: a fake backend, and a client session on the server."""

import json
from contextlib import asynccontextmanager

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from evalve_mcp.backend import Backend
from evalve_mcp.server import build_server

SERVER_URL = "https://evalve.example.test/evalve"
KEY = "KITKEY_secret-credential-value"
LOOP = {"loop_id": "loop-1", "status": "running", "next": {"action": "collect"}}
START = {
    "name": "Refund policy", "cohort_version_id": "cohort-1", "evaluators": [{"ref": "policy"}],
    "tool_policy": {"default": {"type": "recorded"}}, "allowed_changes": ["system_prompt"],
    "allowed_models": [], "required_consecutive_greens": 2, "max_runs": 6, "budget_usd": "5",
}


def answer(status: int = 200, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=LOOP if body is None else body)


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def slow(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


class FakeBackend:
    """Stands in for the Evalve backend. Gives one scripted answer per request, then the default."""

    def __init__(self, *script, health=None):
        self.requests: list[httpx.Request] = []
        self.health_requests: list[httpx.Request] = []
        self.script = list(script)
        self.health = health
        self.sleeps: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evalve/health":
            self.health_requests.append(request)
            if self.health is None:
                return refused(request)
            return self.health(request) if callable(self.health) else self.health
        self.requests.append(request)
        step = self.script.pop(0) if self.script else answer()
        return step(request) if callable(step) else step

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@asynccontextmanager
async def session(fake: FakeBackend, key: str | None = KEY, version: str = "0.1.1"):
    backend = Backend(SERVER_URL, key, version=version, transport=httpx.MockTransport(fake), sleep=fake.sleep)
    async with create_connected_server_and_client_session(build_server(backend)) as client:
        yield client


def text(result) -> str:
    return "\n".join(block.text for block in result.content)
