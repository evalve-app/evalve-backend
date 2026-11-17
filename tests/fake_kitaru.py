"""A small stateful Kitaru server for the Loop steps tests, served through httpx.MockTransport.

It keeps the resources a Loop reads and writes, answers the list filters the backend sends, and
records every request. `fail` makes the next matching request answer with an error.
"""

from __future__ import annotations

import json
import re

import httpx

AGENT_ID = "agent-1"
AGENT_NAME = "support-bot"
AGENT_VERSION_ID = "agent-version-2"
COHORT_ID = "cohort-1"
COHORT_VERSION_ID = "6f1c2c4e-8f4b-4a57-9a51-3c0e2a1d5b10"
EVALUATOR_ID = "evaluator-1"
EVALUATOR_VERSION_ID = "evaluator-version-1"
CONTROL_MODEL = "gpt-small"
MEMBERS = ("recorded-1", "recorded-2")


class FakeKitaru:
    def __init__(self, authorization: str):
        self.authorization = authorization
        self.requests: list[httpx.Request] = []
        self._failures: list[tuple[str, str, httpx.Response]] = []
        self.agents = {AGENT_ID: {"id": AGENT_ID, "name": AGENT_NAME}}
        self.agent_versions = {
            AGENT_VERSION_ID: {
                "id": AGENT_VERSION_ID, "agent_id": AGENT_ID, "version": 2,
                "run_spec": {"command": "python agent.py", "env": {"OPENAI_MODEL": CONTROL_MODEL},
                             "runtime_capabilities": {"overrides": True, "tool_policies": True}},
                "capabilities": {"tools": [], "mcp_servers": [], "skills": []},
            },
        }
        self.cohort_versions = {COHORT_VERSION_ID: {"id": COHORT_VERSION_ID, "cohort_id": COHORT_ID, "session_count": 2}}
        self.cohorts = {COHORT_ID: {"id": COHORT_ID, "agent_id": AGENT_ID}}
        self.sessions = {
            session_id: {"id": session_id, "name": f"Case {n}", "agent_version_id": AGENT_VERSION_ID,
                         "cost": "0.05", "status": "completed", "cohort_version_ids": [COHORT_VERSION_ID]}
            for n, session_id in enumerate(MEMBERS, 1)
        }
        self.evaluators = {EVALUATOR_ID: {"id": EVALUATOR_ID, "name": "policy", "agent_id": None}}
        self.evaluator_versions = {(EVALUATOR_ID, 1): {"id": EVALUATOR_VERSION_ID, "evaluator_id": EVALUATOR_ID, "version": 1}}

    def fail(self, method: str, path_pattern: str, status: int, detail: str = "Failed on purpose.") -> None:
        """Answer the next request whose method and path match with this error."""
        self._failures.append((method, path_pattern, httpx.Response(status, json={"detail": detail})))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != self.authorization:
            return httpx.Response(401, json={"detail": "Not authenticated."})
        path = request.url.path
        for failure in self._failures:
            method, pattern, response = failure
            if method == request.method and re.fullmatch(pattern, path):
                self._failures.remove(failure)
                return response
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if match and method == request.method:
                return handler(request, *match.groups())
        return httpx.Response(404, json={"detail": "Not found."})

    def _routes(self):
        return (
            ("GET", r"/api/v1/cohort-versions/([^/]+)", lambda r, i: self._one(self.cohort_versions, i)),
            ("GET", r"/api/v1/cohorts/([^/]+)", lambda r, i: self._one(self.cohorts, i)),
            ("GET", r"/api/v1/agents/([^/]+)", lambda r, i: self._one(self.agents, i)),
            ("GET", r"/api/v1/agents/([^/]+)/versions", self._agent_versions),
            ("GET", r"/api/v1/agent-versions/([^/]+)", lambda r, i: self._one(self.agent_versions, i)),
            ("GET", r"/api/v1/sessions", lambda r: self._page(r, self.sessions.values(), self._session_field)),
            ("GET", r"/api/v1/evaluators", lambda r: self._page(r, self.evaluators.values())),
            ("GET", r"/api/v1/evaluators/([^/]+)/versions/(\d+)",
             lambda r, i, v: self._one(self.evaluator_versions, (i, int(v)))),
        )

    @staticmethod
    def _one(table: dict, key) -> httpx.Response:
        if key not in table:
            return httpx.Response(404, json={"detail": "Not found."})
        return httpx.Response(200, json=_public(table[key]))

    def _agent_versions(self, request, agent_id):
        if agent_id not in self.agents:
            return httpx.Response(404, json={"detail": "Agent not found."})
        return self._page(request, [v for v in self.agent_versions.values() if v["agent_id"] == agent_id])

    def _session_field(self, session: dict, field: str):
        if field == "cohort_version_id":
            return session["cohort_version_ids"]
        return session.get(field)

    def _page(self, request: httpx.Request, items, field=None) -> httpx.Response:
        field = field or (lambda item, name: item.get(name))
        raw = request.url.params.get("filter")
        matching = [item for item in items if raw is None or _matches(json.loads(raw), item, field)]
        size = int(request.url.params.get("size", 20))
        start = int(request.url.params.get("cursor") or 0)
        end = start + size
        return httpx.Response(200, json={
            "items": [_public(item) for item in matching[start:end]],
            "next_cursor": str(end) if end < len(matching) else None,
        })


def _matches(condition: dict, item: dict, field) -> bool:
    if "and" in condition:
        return all(_matches(part, item, field) for part in condition["and"])
    assert condition["op"] == "eq", condition
    value = field(item, condition["field"])
    return condition["value"] in value if isinstance(value, list) else value == condition["value"]


def _public(item: dict) -> dict:
    # Keys that only the fake uses stay out of answers.
    return {key: value for key, value in item.items() if key != "cohort_version_ids"}
