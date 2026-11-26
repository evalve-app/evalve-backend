"""A small stateful Kitaru server for the Loop steps tests, served through httpx.MockTransport.

It keeps the resources a Loop reads and writes, answers the list filters the backend sends, and
records every request. Idempotency keys behave as in Kitaru 0.27.6: a key is fingerprinted with
the method, path, query and raw body bytes, a retry with the same bytes replays the stored answer,
and other bytes give 422. `expire_keys` stands for the 900 s retention running out. `fail` and
`answer_once` make the next matching request answer with something else.

A started run has one job for each cohort member. A registered worker gets a token that expires
after WORKER_TOKEN_LIFETIME, and a delete answers 204 with no body, as in Kitaru 0.27.6.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from itertools import count

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
WORKER_TOKEN_LIFETIME = timedelta(seconds=3600)


class FakeKitaru:
    def __init__(self, authorization: str, account: dict | None = None):
        self.authorization = authorization
        # Sign-in answers for each accepted Authorization header. Every account can use every resource.
        self.accounts = {authorization: account or {"id": "account-1", "name": "jack", "active": True}}
        # Sign-in answers for each accepted dashboard session: (Cookie header, X-CSRF-Token header).
        self.dashboard_sessions: dict[tuple[str, str], dict] = {}
        self.requests: list[httpx.Request] = []
        self._failures: list[tuple[str, str, httpx.Response]] = []
        self._lost_replies: list[tuple[str, str]] = []
        self.agents = {AGENT_ID: {"id": AGENT_ID, "name": AGENT_NAME}}
        self.agent_versions = {
            AGENT_VERSION_ID: {
                "id": AGENT_VERSION_ID, "agent_id": AGENT_ID, "version": 2,
                "run_spec": {"command": "python agent.py", "env": {"OPENAI_MODEL": CONTROL_MODEL},
                             "runtime_capabilities": {"overrides": True, "tool_policies": True}},
                "capabilities": {"tools": [], "mcp_servers": [], "skills": []},
            },
        }
        self.members = list(MEMBERS)
        self.cohort_versions = {COHORT_VERSION_ID: {"id": COHORT_VERSION_ID, "cohort_id": COHORT_ID, "session_count": 2}}
        self.cohorts = {COHORT_ID: {"id": COHORT_ID, "agent_id": AGENT_ID}}
        self.sessions = {
            session_id: {"id": session_id, "name": f"Case {n}", "agent_version_id": AGENT_VERSION_ID,
                         "cost": "0.05", "status": "completed", "cohort_version_ids": [COHORT_VERSION_ID]}
            for n, session_id in enumerate(MEMBERS, 1)
        }
        self.evaluators = {EVALUATOR_ID: {"id": EVALUATOR_ID, "name": "policy", "agent_id": None}}
        self.evaluator_versions = {(EVALUATOR_ID, 1): {"id": EVALUATOR_VERSION_ID, "evaluator_id": EVALUATOR_ID, "version": 1}}
        self.experiments: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.replays: dict[str, dict] = {}
        self.evaluations: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        # The workers that are registered now, and the token of each worker that was registered.
        self.workers: dict[str, dict] = {}
        self.worker_tokens: dict[str, str] = {}
        self._observers: list[tuple[str, str, object]] = []
        self.keys: dict[str, tuple[str, httpx.Response]] = {}
        # Kitaru's clock and the backend's agree, as on one deployment.
        self.now = datetime.now(timezone.utc)
        self._ids = count(1)

    def add_member(self, session_id: str) -> None:
        """One more session in the cohort version. Call it before the Loop starts."""
        self.members.append(session_id)
        self.sessions[session_id] = {
            "id": session_id, "name": f"Case {len(self.members)}", "agent_version_id": AGENT_VERSION_ID,
            "cost": "0.05", "status": "completed", "cohort_version_ids": [COHORT_VERSION_ID],
        }
        self.cohort_versions[COHORT_VERSION_ID]["session_count"] = len(self.members)

    def fail(self, method: str, path_pattern: str, status: int, detail: str = "Failed on purpose.") -> None:
        """Answer the next request whose method and path match with this error."""
        self.answer_once(method, path_pattern, httpx.Response(status, json={"detail": detail}))

    def answer_once(self, method: str, path_pattern: str, response: httpx.Response) -> None:
        self._failures.append((method, path_pattern, response))

    def observe(self, method: str, path_pattern: str, callback) -> None:
        """Call `callback()` when a matching request arrives, before the fake answers it."""
        self._observers.append((method, path_pattern, callback))

    def expire_keys(self) -> None:
        self.keys.clear()

    def tick(self, seconds: float = 1) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now

    def _new_id(self, kind: str) -> str:
        return f"{kind}-{next(self._ids)}"

    def _stamp(self) -> dict:
        created = self.tick().isoformat()
        return {"created": created, "updated": created}

    def requests_to(self, method: str, path_pattern: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and re.fullmatch(path_pattern, r.url.path)]

    def add_run(self, experiment_id: str, *, cohort_version_id: str = COHORT_VERSION_ID,
                agent_version_id: str = AGENT_VERSION_ID, created: datetime | None = None, status: str = "running") -> dict:
        """A run that someone started outside the backend, for example in the Kitaru UI."""
        run_id = self._new_id("run")
        stamp = {"created": (created or self.tick()).isoformat()}
        self.runs[run_id] = {
            "id": run_id, "experiment_id": experiment_id, "status": status, "number": self._next_number(experiment_id),
            "cohort_version_id": cohort_version_id, "agent_version_id": agent_version_id, "error": None, **stamp,
        }
        return self.runs[run_id]

    def _next_number(self, experiment_id: str) -> int:
        return 1 + max((r["number"] for r in self.runs.values() if r["experiment_id"] == experiment_id), default=0)

    def finish(self, run_id: str, outcome: str = "green", cost: str | None = "0.04") -> None:
        """Complete a run: one replay, result session and verdict per cohort member.

        `outcome` is green (every verdict passes), red (the first member fails) or incomplete (the
        run fails before the second member has a result session).
        """
        run = self.runs[run_id]
        run["status"] = "failed" if outcome == "incomplete" else "completed"
        experiment = self.experiments[run["experiment_id"]]
        for index, baseline_id in enumerate(self.members):
            replay_id, result_id = self._new_id("replay"), self._new_id("session")
            has_result = not (outcome == "incomplete" and index == 1)
            self.replays[replay_id] = {
                "id": replay_id, "experiment_run_id": run_id, "baseline_session_id": baseline_id,
                "result_session_id": result_id if has_result else None,
                "status": "completed" if has_result else "failed", "error": None if has_result else "worker lost",
            }
            if not has_result:
                continue
            self.sessions[result_id] = {
                "id": result_id, "status": "completed", "cost": cost, "outputs": f"reply {index}",
                "experiment_run_id": run_id, "cohort_version_ids": [],
            }
            for spec in experiment["evaluators"]:
                evaluation_id = self._new_id("evaluation")
                self.evaluations[evaluation_id] = {
                    "id": evaluation_id, "session_id": result_id, "experiment_run_id": run_id,
                    "evaluator_version_id": EVALUATOR_VERSION_ID, "evaluator_params": spec.get("params") or {},
                    "passed": not (outcome == "red" and index == 0), "explanation": "checked",
                }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "authorization" in request.headers:
            # As in Kitaru, the header is the credential and the cookie is not a second try.
            account = self.accounts.get(request.headers["authorization"])
        else:
            session = (request.headers.get("cookie"), request.headers.get("x-csrf-token"))
            account = self.dashboard_sessions.get(session)
        if account is None:
            return httpx.Response(401, json={"detail": "Not authenticated."})
        path = request.url.path
        if path == "/api/v1/accounts/me":
            return httpx.Response(200, json=account)
        for method, pattern, callback in self._observers:
            if method == request.method and re.fullmatch(pattern, path):
                callback()
        for failure in self._failures:
            method, pattern, response = failure
            if method == request.method and re.fullmatch(pattern, path):
                self._failures.remove(failure)
                return response
        key = request.headers.get("idempotency-key")
        if key is not None:
            fingerprint = hashlib.sha256(
                b"\n".join([request.method.encode(), path.encode(), request.url.query, request.content])
            ).hexdigest()
            if key in self.keys:
                stored, response = self.keys[key]
                if stored != fingerprint:
                    return httpx.Response(422, json={"detail": "Idempotency-Key was used with a different request."})
                return httpx.Response(response.status_code, content=response.content,
                                      headers={"Idempotent-Replayed": "true", "Content-Type": "application/json"})
        response = self._route(request, path)
        if key is not None and 200 <= response.status_code < 300:
            self.keys[key] = (fingerprint, response)
        for lost in self._lost_replies:
            if lost[0] == request.method and re.fullmatch(lost[1], path):
                self._lost_replies.remove(lost)
                raise httpx.ReadError("The reply was lost.", request=request)
        return response

    def lose_next_reply(self, method: str, path_pattern: str) -> None:
        """Do the next matching request, then drop its reply, as a network cut would."""
        self._lost_replies.append((method, path_pattern))

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
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
            ("GET", r"/api/v1/sessions", self._sessions),
            ("GET", r"/api/v1/evaluators", lambda r: self._page(r, self.evaluators.values())),
            ("GET", r"/api/v1/evaluators/([^/]+)/versions/(\d+)",
             lambda r, i, v: self._one(self.evaluator_versions, (i, int(v)))),
            ("POST", r"/api/v1/experiments", self._create_experiment),
            ("GET", r"/api/v1/experiments", lambda r: self._page(r, self.experiments.values())),
            ("GET", r"/api/v1/experiments/([^/]+)", lambda r, i: self._one(self.experiments, i)),
            ("PATCH", r"/api/v1/experiments/([^/]+)", self._update_experiment),
            ("POST", r"/api/v1/experiments/([^/]+)/runs", self._start_run),
            ("GET", r"/api/v1/experiment-runs", lambda r: self._page(r, self.runs.values())),
            ("GET", r"/api/v1/experiment-runs/([^/]+)", lambda r, i: self._one(self.runs, i)),
            ("POST", r"/api/v1/experiment-runs/([^/]+)/cancel", self._cancel_run),
            ("GET", r"/api/v1/experiment-runs/([^/]+)/jobs", self._run_jobs),
            ("POST", r"/api/v1/workers", self._register_worker),
            ("DELETE", r"/api/v1/workers/([^/]+)", self._delete_worker),
            ("GET", r"/api/v1/replays", lambda r: self._page(r, self.replays.values())),
            ("GET", r"/api/v1/evaluations", lambda r: self._page(r, self.evaluations.values())),
        )

    def _create_experiment(self, request):
        body = json.loads(request.content)
        if any(e["agent_id"] == body["agent_id"] and e["name"] == body["name"] for e in self.experiments.values()):
            return httpx.Response(409, json={"detail": f"Experiment name '{body['name']}' is already registered"})
        experiment_id = self._new_id("experiment")
        evaluators = [{"evaluator": e["evaluator"], "version": e.get("version"), "params": e.get("params", {}),
                       "connection_id": e.get("connection_id")} for e in body["evaluators"]]
        override = {"model": None, "system_prompt": None, "prompt": None, "model_params": None} | (body.get("override") or {})
        self.experiments[experiment_id] = {
            "id": experiment_id, "name": body["name"], "agent_id": body["agent_id"], "description": body.get("description"),
            "override": override, "tool_policy": body.get("tool_policy"), "evaluators": evaluators, **self._stamp(),
        }
        return httpx.Response(201, json=self.experiments[experiment_id])

    def _update_experiment(self, request, experiment_id):
        if experiment_id not in self.experiments:
            return httpx.Response(404, json={"detail": "Experiment not found."})
        self.experiments[experiment_id].update(json.loads(request.content))
        return httpx.Response(200, json=self.experiments[experiment_id])

    def _start_run(self, request, experiment_id):
        if experiment_id not in self.experiments:
            return httpx.Response(404, json={"detail": "Experiment not found."})
        body = json.loads(request.content)
        run = self.add_run(experiment_id, cohort_version_id=body["cohort_version_id"],
                           agent_version_id=body["agent_version_id"])
        # As in Kitaru, the start call makes one job for each session of the cohort version.
        for _member in self.members:
            job_id = self._new_id("job")
            self.jobs[job_id] = {"id": job_id, "kind": "replay", "status": "running", "experiment_run_id": run["id"]}
        return httpx.Response(201, json=run)

    def _run_jobs(self, request, run_id):
        if run_id not in self.runs:
            return httpx.Response(404, json={"detail": "Experiment run not found."})
        return self._page(request, [job for job in self.jobs.values() if job["experiment_run_id"] == run_id])

    def _register_worker(self, request):
        body = json.loads(request.content)
        scope = body.get("scope") or {}
        # The request model of Kitaru: a name, a scope with 1 to 16 claims, and a runtime with a platform.
        valid = (
            isinstance(body.get("name"), str) and isinstance((body.get("runtime") or {}).get("platform"), str)
            and set(body) <= {"name", "scope", "runtime", "metadata"} and set(scope) <= {"claims", "selectors", "job_id"}
            and 1 <= len(scope.get("claims") or []) <= 16 and scope.get("selectors") != []
        )
        if not valid:
            return httpx.Response(422, json={"detail": "Invalid worker."})
        worker_id = self._new_id("worker")
        self.workers[worker_id] = {"id": worker_id, "name": body["name"], "scope": scope, "runtime": body["runtime"]}
        self.worker_tokens[worker_id] = f"worker-token-value-{worker_id}"
        expires = self.tick() + WORKER_TOKEN_LIFETIME
        return httpx.Response(200, json={
            "worker": self.workers[worker_id], "token": self.worker_tokens[worker_id],
            "token_expires_at": expires.isoformat().replace("+00:00", "Z"),
        })

    def _delete_worker(self, request, worker_id):
        if self.workers.pop(worker_id, None) is None:
            return httpx.Response(404, json={"detail": "Worker not found."})
        return httpx.Response(204)

    def _cancel_run(self, request, run_id):
        run = self.runs.get(run_id)
        if run is None:
            return httpx.Response(404, json={"detail": "Experiment run not found."})
        if run["status"] != "running":
            return httpx.Response(409, json={"detail": "Experiment run is not running."})
        run["status"] = "canceled"
        return httpx.Response(200, json=run)

    @staticmethod
    def _one(table: dict, key) -> httpx.Response:
        if key not in table:
            return httpx.Response(404, json={"detail": "Not found."})
        return httpx.Response(200, json=_public(table[key]))

    def _agent_versions(self, request, agent_id):
        if agent_id not in self.agents:
            return httpx.Response(404, json={"detail": "Agent not found."})
        return self._page(request, [v for v in self.agent_versions.values() if v["agent_id"] == agent_id])

    @staticmethod
    def _session_field(session: dict, field: str):
        if field == "cohort_version_id":
            return session["cohort_version_ids"]
        return session.get(field)

    def _sessions(self, request: httpx.Request) -> httpx.Response:
        # As in Kitaru, a session list has no inputs or outputs unless include_payloads is set.
        payloads = request.url.params.get("include_payloads") == "true"
        return self._page(request, self.sessions.values(), self._session_field, ({} if payloads else {"inputs", "outputs"}))

    def _page(self, request: httpx.Request, items, field=None, leave_out=frozenset()) -> httpx.Response:
        field = field or (lambda item, name: item.get(name))
        raw = request.url.params.get("filter")
        matching = [item for item in items if raw is None or _matches(json.loads(raw), item, field)]
        size = int(request.url.params.get("size", 20))
        start = int(request.url.params.get("cursor") or 0)
        end = start + size
        page = [{key: value for key, value in _public(item).items() if key not in leave_out} for item in matching[start:end]]
        return httpx.Response(200, json={"items": page, "next_cursor": str(end) if end < len(matching) else None})


def _matches(condition: dict, item: dict, field) -> bool:
    if "and" in condition:
        return all(_matches(part, item, field) for part in condition["and"])
    assert condition["op"] == "eq", condition
    value = field(item, condition["field"])
    return condition["value"] in value if isinstance(value, list) else value == condition["value"]


def _public(item: dict) -> dict:
    # Keys that only the fake uses stay out of answers. Kitaru's session has no experiment_run_id.
    hidden = {"cohort_version_ids"} | ({"experiment_run_id"} if "outputs" in item else set())
    return {key: value for key, value in item.items() if key not in hidden}
