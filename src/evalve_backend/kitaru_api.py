"""The Kitaru calls of the Loop steps. Each call uses the caller's credential.

The credential is an Authorization header, or the dashboard session cookie with its CSRF token.
It is passed through for one request and is never stored or logged. Bodies are never logged
either: a log line names only the method and the HTTP status. Kitaru fingerprints the raw body bytes
of a request with an Idempotency-Key, so every body is written by one canonical serializer.
"""

from __future__ import annotations

import json
import logging

import httpx

from evalve_backend.auth import Credential

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
IN_PROGRESS = "still in progress"
# What a worker of a Loop task reports as its runtime. Kitaru stores the text and does not read it.
WORKER_PLATFORM = "fargate"


class KitaruTransient(Exception):
    """Kitaru could not answer now. The same call can succeed later."""


class KitaruRejected(Exception):
    """Kitaru refused the call. Repeating it gives the same answer."""

    def __init__(self, status: int, detail: str, request: str = ""):
        super().__init__(f"Kitaru refused the request (HTTP {status}): {detail}")
        self.status = status
        self.detail = detail
        # The method and the route template, for example "POST /experiments/{id}/runs". It has no IDs.
        self.request = request

    def describe(self) -> str:
        """The text for a Loop's reason, which every account can read."""
        where = f"{self.request}, " if self.request else ""
        return f"{where}HTTP {self.status}: {self.detail}"


class KitaruNotFound(KitaruRejected):
    """The resource does not exist, or no longer exists."""


class KitaruConflict(KitaruRejected):
    """A 409 that is not an idempotency key still in progress, for example a duplicate name."""


def canonical(body: dict) -> bytes:
    """Sorted keys, compact separators, UTF-8. The same stored values always give the same bytes."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def condition(field: str, value: str) -> dict:
    return {"field": field, "op": "eq", "value": value}


def _validation_errors(detail: list) -> str:
    """The field and the message of each validation error that Kitaru (FastAPI) returned for a 422.

    An error object can also hold `input` and `ctx`, which repeat what the caller sent, for example a
    system prompt. Those never go into the text.
    """
    errors = []
    for item in detail:
        message = item.get("msg") if isinstance(item, dict) else None
        if not isinstance(message, str) or not message:
            continue
        location = item.get("loc")
        field = ".".join(str(part) for part in location) if isinstance(location, list) else ""
        errors.append(f"{field}: {message}" if field else message)
    return "; ".join(errors)


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):
        detail = _validation_errors(detail)
    if not isinstance(detail, str) or not detail:
        detail = "no details"
    # The detail can end up in a Loop's reason, which every account can read. Keep it short.
    return detail[:300]


def _route(method: str, path: str) -> str:
    """The method and the route template of a call. Each path segment with a digit is an ID."""
    segments = path.removeprefix("/api/v1").split("/")
    return f"{method} " + "/".join("{id}" if any(ch.isdigit() for ch in s) else s for s in segments)


class KitaruApi:
    """The Kitaru REST calls that a Loop needs, made with one caller's credential."""

    def __init__(self, client: httpx.AsyncClient, credential: Credential):
        self._client = client
        self._credential = credential

    def __repr__(self) -> str:
        return "KitaruApi()"

    async def _send(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                    idempotency_key: str | None = None) -> dict:
        headers = dict(self._credential.headers)
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = canonical(body)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(method, path, params=params, content=content, headers=headers)
        except httpx.HTTPError as error:
            # An httpx error can carry the request and its headers. Log the kind only.
            logger.warning("Kitaru did not answer a %s request (%s).", method, type(error).__name__)
            raise KitaruTransient from None
        status = response.status_code
        if status == 204:
            # A delete answers with no body.
            return {}
        if 200 <= status < 300:
            try:
                return response.json()
            except ValueError:
                logger.warning("Kitaru answered a %s request with a body that is not JSON.", method)
                raise KitaruTransient from None
        logger.warning("Kitaru answered a %s request with HTTP %d.", method, status)
        detail = _detail(response)
        # 401 and 403: the caller's credential stopped working during the step. That is not a fault
        # of the Loop, so the caller tries again after signing in. 408 and 429 come from a proxy.
        if status >= 500 or status in (401, 403, 408, 429) or (status == 409 and IN_PROGRESS in detail):
            raise KitaruTransient
        request = _route(method, path)
        if status == 404:
            raise KitaruNotFound(status, detail, request)
        if status == 409:
            raise KitaruConflict(status, detail, request)
        raise KitaruRejected(status, detail, request)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._send("GET", path, params=params)

    async def _list(self, path: str, filter: dict | None = None, **extra) -> list[dict]:
        items, cursor = [], None
        while True:
            params = {"size": PAGE_SIZE, **extra}
            if filter is not None:
                params["filter"] = json.dumps(filter, sort_keys=True, separators=(",", ":"))
            if cursor:
                params["cursor"] = cursor
            page = await self._get(path, params)
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return items

    # Setup reads, for the preflight.

    async def get_cohort_version(self, cohort_version_id: str) -> dict:
        return await self._get(f"/api/v1/cohort-versions/{cohort_version_id}")

    async def get_cohort(self, cohort_id: str) -> dict:
        return await self._get(f"/api/v1/cohorts/{cohort_id}")

    async def list_cohort_sessions(self, cohort_version_id: str) -> list[dict]:
        return await self._list("/api/v1/sessions", condition("cohort_version_id", cohort_version_id))

    async def get_agent(self, agent_id: str) -> dict:
        return await self._get(f"/api/v1/agents/{agent_id}")

    async def list_agent_versions(self, agent_id: str) -> list[dict]:
        return await self._list(f"/api/v1/agents/{agent_id}/versions")

    async def get_agent_version(self, agent_version_id: str) -> dict:
        return await self._get(f"/api/v1/agent-versions/{agent_version_id}")

    async def find_evaluator(self, name: str) -> dict | None:
        """Evaluator names are unique on the whole server, so there is at most one."""
        found = await self._list("/api/v1/evaluators", condition("name", name))
        return found[0] if found else None

    async def get_evaluator_version(self, evaluator_id: str, version: int) -> dict:
        return await self._get(f"/api/v1/evaluators/{evaluator_id}/versions/{version}")

    # Experiments and runs.

    async def create_experiment(self, body: dict, idempotency_key: str) -> dict:
        return await self._send("POST", "/api/v1/experiments", body=body, idempotency_key=idempotency_key)

    async def find_experiment(self, agent_id: str, name: str) -> dict | None:
        """Experiment names are unique per agent, so the lookup filters on both."""
        found = await self._list("/api/v1/experiments", {"and": [condition("agent_id", agent_id), condition("name", name)]})
        return found[0] if found else None

    async def get_experiment(self, experiment_id: str) -> dict:
        return await self._get(f"/api/v1/experiments/{experiment_id}")

    async def update_description(self, experiment_id: str, description: str) -> dict:
        return await self._send("PATCH", f"/api/v1/experiments/{experiment_id}", body={"description": description})

    async def start_run(self, experiment_id: str, body: dict, idempotency_key: str) -> dict:
        return await self._send("POST", f"/api/v1/experiments/{experiment_id}/runs", body=body,
                                idempotency_key=idempotency_key)

    async def get_run(self, run_id: str) -> dict:
        return await self._get(f"/api/v1/experiment-runs/{run_id}")

    async def list_runs(self, experiment_id: str) -> list[dict]:
        return await self._list("/api/v1/experiment-runs", condition("experiment_id", experiment_id))

    async def cancel_run(self, run_id: str) -> dict:
        return await self._send("POST", f"/api/v1/experiment-runs/{run_id}/cancel")

    # The workers of one run. A worker token is in the reply of a registration and nowhere else.

    async def list_run_jobs(self, run_id: str) -> list[dict]:
        """The jobs of a run. Kitaru makes one job for each session inside the start call."""
        return await self._list(f"/api/v1/experiment-runs/{run_id}/jobs")

    async def register_worker(self, name: str, job_id: str) -> dict:
        """Register a worker that claims only the agent task and the evaluator tasks of one job.

        The reply has `worker` (with its `id`), `token` and `token_expires_at`. Kitaru makes a new
        worker for each call, so a call that is sent again leaves two workers.
        """
        scope = {"claims": [{"kind": "agent"}, {"kind": "evaluator"}], "job_id": job_id}
        return await self._send("POST", "/api/v1/workers", body={
            "name": name, "scope": scope, "runtime": {"platform": WORKER_PLATFORM},
        })

    async def delete_worker(self, worker_id: str) -> None:
        await self._send("DELETE", f"/api/v1/workers/{worker_id}")

    async def list_tasks(self, job_id: str, kind: str | None = None) -> list[dict]:
        """The tasks of one job, each page. The agent task and the evaluator tasks of one Replay have
        the same job. A task has `kind`, `status` and `worker_id`: the worker of its last attempt."""
        of_job = condition("job_id", job_id)
        return await self._list("/api/v1/tasks", of_job if kind is None else {"and": [of_job, condition("kind", kind)]})

    # The evidence of one run.

    async def list_replays(self, run_id: str) -> list[dict]:
        return await self._list("/api/v1/replays", condition("experiment_run_id", run_id))

    async def list_evaluations(self, run_id: str) -> list[dict]:
        return await self._list("/api/v1/evaluations", condition("experiment_run_id", run_id))

    async def list_result_sessions(self, run_id: str) -> list[dict]:
        """One paged call for every result session of the run, with `cost`. Baseline sessions stay out.
        A session list leaves out `inputs` and `outputs` unless asked, and `outputs` is the reply."""
        return await self._list("/api/v1/sessions", condition("experiment_run_id", run_id), include_payloads="true")
