"""The v1 Loop routes. Reads are shared; only the Loop owner moves a Loop.

A Bearer credential of the Loop owner drives the Loop (starts a Run). A dashboard session of the
Loop owner refreshes it: it records a finished Run and applies an end state, and it starts no Run.
"""

import base64
from collections.abc import Awaitable, Callable
import json
import re
from asyncio import to_thread
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from evalve_backend import loop_models as models, loop_rules
from evalve_backend.auth import Account, Credential, request_credential, require_account, verify_account
from evalve_backend.loop_steps import LoopSteps, PreflightRejected, StepUnavailable
from evalve_backend.store import LoopStart, RunPlan, Store


class LoopRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handle = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                response = await handle(request)
            except RequestValidationError as error:
                problem = error.errors()[0]
                await self.check_caller(request, problem)
                location = ".".join(str(part) for part in problem["loc"])
                raise HTTPException(422, f"Invalid or missing input at {location}.") from None
            response.headers["Cache-Control"] = "no-store"
            return response

        return route

    async def check_caller(self, request: Request, problem: dict) -> None:
        """Refuse a caller that has no good credential, before it gets a validation error."""
        if problem["type"] == "json_invalid":
            # FastAPI decodes JSON before calling dependencies. Check
            # identity here, too, before returning a validation error.
            await verify_account(request)


def errors(*statuses: int) -> dict:
    return {status: {"model": models.ApiError} for status in statuses}


router = APIRouter(
    prefix="/v1/loops", tags=["Loops"], route_class=LoopRoute, responses=errors(401, 422, 503),
)


@dataclass(frozen=True)
class Caller:
    account: Account
    credential: Credential = field(repr=False)

    @property
    def id(self) -> str:
        return self.account.id

    @property
    def kind(self) -> Literal["bearer", "session"]:
        return self.credential.kind


ONLY_BEARER_DRIVES = "Only a Bearer credential can start a Run. Use the Evalve MCP."


def can_drive(caller: Caller, loop: dict | None) -> bool:
    """True when this caller can start a Run of the Loop: a Bearer credential of the Loop owner.

    `loop` is None for the Loop that the call would start, which the caller would own. Read the
    type of a credential only through this function.
    """
    return caller.kind == "bearer" and (loop is None or loop["owner_account_id"] == caller.id)


def _require_bearer(caller: Caller) -> None:
    """Refuse a dashboard session on a route that starts a Run, before the route does anything."""
    if not can_drive(caller, None):
        raise HTTPException(403, ONLY_BEARER_DRIVES)


def get_caller(request: Request, account: Annotated[Account, Depends(require_account)]) -> Caller:
    # require_account has accepted this credential, so it is not None here.
    return Caller(account, request_credential(request))


def get_store(request: Request) -> Store:
    return request.app.state.loop_store


def get_steps(request: Request) -> LoopSteps | None:
    return request.app.state.loop_steps


SignedIn = Annotated[Caller, Depends(get_caller)]
LoopStore = Annotated[Store, Depends(get_store)]
Steps = Annotated[LoopSteps | None, Depends(get_steps)]
ExperimentNumber = Annotated[int, Path(ge=0)]
RunNumber = Annotated[int, Path(ge=1)]


def _decimal_string(amount: Decimal) -> str:
    if amount.is_zero():
        return "0"
    sign, digits, exponent = amount.as_tuple()
    significant = tuple(map(int, "".join(map(str, digits)).rstrip("0")))
    return str(Decimal((sign, significant, exponent + len(digits) - len(significant))))


def _setup(body: models.StartLoop) -> dict:
    # Serialize only validated fields. Defaults and equivalent money values are
    # identical for retry purposes. Keep list order and preserve prompt text.
    cfg = body.model_dump(mode="json", exclude_none=True)
    cfg["budget_usd"] = _decimal_string(body.budget_usd)
    if body.control_estimate_usd is not None:
        cfg["control_estimate_usd"] = _decimal_string(body.control_estimate_usd)
    try:
        cfg = loop_rules.validate_config(cfg)
    except loop_rules.ConfigError as error:
        raise HTTPException(422, str(error)) from None
    policies = [("default", cfg["tool_policy"]["default"]), *cfg["tool_policy"]["tools"].items()]
    if cfg["allow_live_tools"] or any(
        policy["type"] == "passthrough" or policy.get("on_miss") == "passthrough" for _tool, policy in policies
    ):
        raise HTTPException(422, "Live tools are disabled for Loops in v1.")
    # An allow-list: a Loop also refuses a type or a scope that a later Kitaru version adds.
    for tool, policy in policies:
        if policy["type"] not in ("static", "history"):
            raise HTTPException(
                422,
                f"A Loop does not permit a tool policy of the type `{policy['type']}`. "
                "The permitted types are `static` and `history`.",
            )
        scope = policy.get("scope")
        if policy["type"] == "history" and scope != "baseline":
            found = "no scope" if scope is None else f"the scope `{scope}`"
            raise HTTPException(
                422,
                "A Loop permits a `history` tool policy only with the scope `baseline`. "
                f"The policy for `{tool}` has {found}.",
            )
    return cfg


def _request_hash(config: dict) -> str:
    try:
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise HTTPException(422, "Loop settings must contain valid JSON values.") from None
    return sha256(canonical.encode()).hexdigest()


def _execution(steps: LoopSteps | None) -> LoopSteps:
    if steps is None:
        raise HTTPException(503, "Loop execution is unavailable. Try again.")
    return steps


T = TypeVar("T")


async def _step(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except StepUnavailable:
        raise HTTPException(503, "Kitaru could not finish the Loop request. Try again.") from None


async def _load(store: Store, loop_id: str, *, owner: str | None = None) -> dict:
    loop = await to_thread(store.get_loop, owner, loop_id)
    if loop is None:
        raise HTTPException(404, "Loop not found.")
    return loop


def _choose(loop: dict, *, now: datetime | None = None) -> dict:
    return loop_rules.decide(loop["config"], loop, now, loop["waiting_since"])


async def _expire(store: Store, loop: dict, decision: dict) -> bool:
    return await to_thread(
        store.expire_waiting_loop, loop["owner_account_id"], loop["loop_id"], loop["waiting_since"], decision["reason"],
    )


def _winner(loop: dict) -> dict | None:
    if loop["outcome"] != "passed":
        return None
    experiment = loop["experiments"][-1]
    return {
        "experiment": experiment["number"], "experiment_id": experiment["experiment_id"],
        "override": experiment["override"], "change": experiment["change"],
    }


def _totals(loop: dict) -> dict:
    recorded, accounted = loop_rules.accounted_spend(loop)
    return {
        "spend": {"recorded_usd": recorded, "accounted_usd": accounted, "budget_usd": loop["config"]["budget_usd"]},
        "runs": {"used": sum(len(e["runs"]) for e in loop["experiments"]), "max": loop["config"]["max_runs"]},
        "winner": _winner(loop),
    }


def _failed_cases(run: dict | None) -> list[dict]:
    return [row for row in (run or {}).get("evidence") or [] if row["passed"] is False]


async def _detail(store: Store, loop: dict) -> dict:
    decision = _choose(loop) if loop["status"] == "running" else None
    needs = None
    if decision and decision["action"] == "needs_change":
        experiment = loop["experiments"][-1]
        red = next(run for run in reversed(experiment["runs"]) if (run["result"] or {}).get("outcome") == "red")
        evidence = await to_thread(store.get_run, None, loop["loop_id"], experiment["number"], red["number"])
        since = loop["waiting_since"]
        needs = {
            "experiment": decision["experiment"], "allowed_changes": loop["config"]["allowed_changes"],
            "allowed_models": loop["config"]["allowed_models"],
            "overrides_tried": [e["override"] for e in loop["experiments"] if e["number"] > 0],
            "failed_cases": _failed_cases(evidence), "idle_deadline": since + loop_rules.IDLE_LIMIT if since else None,
        }
    return {**loop, **_totals(loop), "setup": loop["config"], "next": decision, "needs": needs}


def _list_item(loop: dict) -> dict:
    """One row of the Loop list, from the same rules as the status call, so the two agree."""
    return {
        **loop, "hypothesis": loop["config"].get("hypothesis"), "experiments": len(loop["experiments"]),
        "spend": _totals(loop)["spend"],
        "next_action": _choose(loop)["action"] if loop["status"] == "running" else None,
    }


def _cursor(item: dict) -> str:
    raw = json.dumps([item["created_at"].isoformat(), item["loop_id"]]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _before(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        position = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(position, list) or len(position) != 2 or not all(isinstance(part, str) for part in position):
            raise ValueError
        timestamp, loop_id = position
        instant = datetime.fromisoformat(timestamp)
        if instant.tzinfo is None:
            raise ValueError
        return instant, str(UUID(loop_id))
    except ValueError:
        raise HTTPException(422, "Invalid Loop list cursor.") from None


async def _submit_first(
    store: Store, steps: LoopSteps | None, caller: Caller, loop: dict, experiment: dict,
) -> dict:
    first = experiment["runs"][0]
    if first["run_id"] is None and loop["status"] == "running":
        await _step(_execution(steps).submit_pending(caller.id, loop["loop_id"], experiment["number"], caller.credential))
        loop = await _load(store, loop["loop_id"], owner=caller.id)
        first = loop["experiments"][experiment["number"]]["runs"][0]
        if first["run_id"] is None and loop["status"] == "running":
            raise HTTPException(503, "Kitaru has not confirmed the first Run. Try again.")
    return loop


@router.post(
    "", status_code=201, response_model=models.LoopDetail,
    responses={200: {"model": models.LoopDetail, "description": "Start request replayed"}, **errors(403, 409)},
)
async def start_loop(
    body: models.StartLoop, response: Response, caller: SignedIn, store: LoopStore, steps: Steps,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
):
    """Validate setup and start the Control Experiment. Repeat safely with the same key.

    Only a Bearer credential can start a Run. A dashboard session gets 403.
    """
    _require_bearer(caller)
    if not idempotency_key.strip() or not idempotency_key.isascii() or not idempotency_key.isprintable():
        raise HTTPException(422, "Idempotency-Key must be nonempty printable ASCII.")
    config = _setup(body)
    request_hash = _request_hash(config)
    result = await to_thread(store.find_start, caller.id, idempotency_key, request_hash)
    if result is None:
        try:
            prepared = await _step(_execution(steps).preflight(config, caller.account, caller.credential))
        except PreflightRejected as error:
            raise HTTPException(422, str(error)) from None
        slug = re.sub(r"[^a-z0-9]+", "-", config["name"].lower()).strip("-")
        start = LoopStart(caller.id, config["name"], slug, prepared.agent_id, config, prepared.preflight,
                          idempotency_key, request_hash)
        result = await to_thread(store.start_loop, start, prepared.first_run)
    if result.disposition == "key_conflict":
        raise HTTPException(409, "This Idempotency-Key was already used with different Loop settings.")
    if result.disposition == "slug_conflict":
        raise HTTPException(409, f"Loop name {config['name']!r} is already in use for this agent.")
    assert result.loop_id is not None
    loop = await _load(store, result.loop_id, owner=caller.id)
    loop = await _submit_first(store, steps, caller, loop, loop["experiments"][0])
    if result.disposition == "replayed":
        response.status_code = 200
    elif loop["experiments"][0]["runs"][0]["run_id"] is None:
        raise HTTPException(422, loop["reason"] or "The Loop ended before the Control Run started.")
    return await _detail(store, loop)


@router.get("", response_model=models.LoopList)
async def list_loops(
    caller: SignedIn, store: LoopStore, status: models.LoopStatus | None = None,
    owner_account_id: Annotated[str | None, Query(min_length=1, description="Filter by the Loop owner's Kitaru account ID.")] = None,
    agent_id: Annotated[str | None, Query(min_length=1, description="Filter by the Kitaru agent ID of the Loop.")] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    """List Loops across the deployment, newest first.

    The list reads the Loop store only. It makes no Kitaru call apart from the identity check, and
    it does not move a Loop.
    """
    rows = await to_thread(
        store.list_loops, owner_account_id, status=status, agent_id=agent_id, limit=limit + 1, before=_before(cursor),
    )
    return {
        "items": [_list_item(loop) for loop in rows[:limit]],
        "next_cursor": _cursor(rows[limit - 1]) if len(rows) > limit else None,
    }


@router.get("/{loop_id}", response_model=models.LoopDetail, responses=errors(404))
async def loop_status(loop_id: str, caller: SignedIn, store: LoopStore, steps: Steps):
    """Read saved status. A call from the Loop owner also moves a running Loop.

    With a Bearer credential, the call can drive the Loop: it records a finished Run and starts the
    next Run. With a dashboard session, the call can only refresh the Loop: it records a finished Run
    and applies an end state, and it starts no Run. After a refresh, `next.action` can be
    `start_control`, `resume_start`, `replay` or `retry`: the Loop waits for a call that drives it.
    The wait limit applies to the two types of credential.
    """
    loop = await _load(store, loop_id)
    if loop["owner_account_id"] == caller.id and loop["status"] == "running":
        decision = _choose(loop, now=datetime.now(timezone.utc))
        if decision["action"] == "stop_idle":
            await _expire(store, loop, decision)
            loop = await _load(store, loop_id)
        elif steps is not None and decision["action"] != "needs_change":
            move = steps.advance if can_drive(caller, loop) else steps.refresh
            await _step(move(caller.id, loop_id, caller.credential))
            loop = await _load(store, loop_id)
    return await _detail(store, loop)


@router.get(
    "/{loop_id}/experiments/{experiment}/runs/{run}", response_model=models.RunDetail, responses=errors(404),
)
async def run_evidence(loop_id: str, experiment: ExperimentNumber, run: RunNumber, caller: SignedIn, store: LoopStore):
    """Read the evidence collected for one Run."""
    item = await to_thread(store.get_run, None, loop_id, experiment, run)
    if item is None:
        raise HTTPException(404, "Run not found.")
    return {**item, "failed_cases": _failed_cases(item)}


def _proposal(loop: dict, body: models.ProposeExperiment, override: dict, now: datetime) -> tuple[dict, RunPlan]:
    config = loop["config"]
    preflight = loop["preflight"]
    state = {
        **loop, "control_model": preflight.get("control_model") or config.get("control_model"),
        "control_prompt": preflight.get("control_prompt", config.get("control_prompt")),
    }
    try:
        candidate = loop_rules.validate_candidate(config, state, override, body.change, body.control_model)
    except loop_rules.CandidateError as error:
        raise HTTPException(422, str(error)) from None
    estimate = loop_rules.estimate_run(state, override)
    if estimate is None and body.estimate_usd is None:
        raise HTTPException(422, "Provide an estimate for this proposed Experiment.")
    estimate = max(value for value in (estimate, body.estimate_usd) if value is not None)
    plan = RunPlan(estimate, config["cohort_version_id"], preflight["agent_version_id"], now)
    return candidate, plan


def _existing_experiment(loop: dict, number: int, override: dict) -> dict | None:
    existing = next((item for item in loop["experiments"] if item["number"] == number), None)
    if existing and existing["override"] != override:
        raise HTTPException(409, "This Experiment number already has a different change.")
    return existing


@router.post(
    "/{loop_id}/experiments", status_code=201, response_model=models.ExperimentAccepted,
    responses=errors(403, 404, 409),
)
async def propose_experiment(
    loop_id: str, body: models.ProposeExperiment, caller: SignedIn, store: LoopStore, steps: Steps,
):
    """Accept one proposed change and start the Experiment's first Run.

    Only a Bearer credential can start a Run. A dashboard session gets 403.
    """
    _require_bearer(caller)
    loop = await _load(store, loop_id, owner=caller.id)
    override = {key: value for key, value in (("model", body.model), ("system_prompt", body.system_prompt))
                if value is not None}
    existing = _existing_experiment(loop, body.experiment, override)
    if existing is None:
        now = datetime.now(timezone.utc)
        decision = _choose(loop, now=now)
        if loop["status"] == "running" and decision["action"] == "stop_idle":
            if await _expire(store, loop, decision):
                raise HTTPException(409, decision["reason"])
            loop = await _load(store, loop_id, owner=caller.id)
            decision = _choose(loop, now=now)
        if loop["status"] == "running" and (decision["action"], decision.get("experiment")) == ("needs_change", body.experiment):
            candidate, plan = _proposal(loop, body, override, now)
            _execution(steps)
            await to_thread(
                store.add_experiment, caller.id, loop_id, body.experiment, override, candidate["change"], plan,
                control_model=candidate["control_model"],
            )
            loop = await _load(store, loop_id, owner=caller.id)
        existing = _existing_experiment(loop, body.experiment, override)
        if existing is None:
            raise HTTPException(409, "The Loop is not waiting for this Experiment.")
    loop = await _submit_first(store, steps, caller, loop, existing)
    experiment_id = loop["experiments"][body.experiment]["experiment_id"]
    if experiment_id is None:
        raise HTTPException(409, "This Experiment did not start because the Loop ended.")
    return {"experiment_id": experiment_id}


@router.post("/{loop_id}/cancel", response_model=models.LoopDetail, responses=errors(404))
async def cancel_loop(loop_id: str, caller: SignedIn, store: LoopStore, steps: Steps):
    """Cancel a Loop that belongs to the caller. A terminal Loop stays terminal."""
    loop = await _load(store, loop_id, owner=caller.id)
    if loop["status"] == "running":
        await _step(_execution(steps).cancel(caller.id, loop_id, caller.credential))
        loop = await _load(store, loop_id, owner=caller.id)
        if loop["status"] == "running":
            raise HTTPException(503, "Kitaru has not confirmed cancellation. Try again.")
    return await _detail(store, loop)


@router.get("/{loop_id}/report", response_model=models.LoopReport, responses=errors(404, 409))
async def final_report(loop_id: str, caller: SignedIn, store: LoopStore):
    """Read the final outcome and winning change after a Loop ends."""
    loop = await _load(store, loop_id)
    if loop["status"] == "running":
        raise HTTPException(409, "The Loop is still running. Check its status.")
    return {**loop, **_totals(loop)}
