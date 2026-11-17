"""Where the Runs of a Loop execute. KitaruExecutor uses the Kitaru workers; Fargate can replace it (sc-283).

The store is the record of which experiment and runs belong to a Loop (sc-315). Kitaru idempotency
keys expire after 900 s and a run has no field for a marker, so the executor finds its own work
again by the stored names, ids and times, never by the run `number`. Scoring stays in
`loop_rules.score_run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from evalve_backend import loop_rules
from evalve_backend.kitaru_api import KitaruApi, KitaruConflict, KitaruNotFound, KitaruTransient

# A run created this long before its Run row's submit started can still be the one it submitted.
ADOPTION_WINDOW = timedelta(seconds=60)


class ExecutionFailed(Exception):
    """The Loop cannot go on. The message is the plain reason the Loop is failed with."""


class RunGone(Exception):
    """A recorded run no longer exists. The Run counts as incomplete."""


@dataclass(frozen=True)
class ExperimentSpec:
    """Everything that identifies one Experiment, from stored values only."""
    name: str
    idempotency_key: str
    agent_id: str
    description: str
    override: dict
    tool_policy: dict
    evaluators: list[dict]
    experiment_id: str | None


@dataclass(frozen=True)
class RunSpec:
    idempotency_key: str
    cohort_version_id: str
    agent_version_id: str
    submit_started_at: datetime


@dataclass(frozen=True)
class RunEvidence:
    replays: list[dict]
    evaluations: list[dict]
    sessions: dict[str, dict]


class ExperimentExecutor(Protocol):
    async def ensure_experiment(self, spec: ExperimentSpec) -> str:
        """Return the id of the Experiment, creating it when the store has none."""
        ...

    async def submit(self, experiment_id: str, run: RunSpec, known_run_ids: set[str]) -> str:
        """Return the id of the run: an adopted one, or a new one."""
        ...

    async def poll(self, run_id: str) -> dict: ...

    async def fetch_results(self, run_id: str) -> RunEvidence: ...

    async def cancel(self, run_id: str) -> None: ...


def _without_nulls(value):
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


def evaluator_configs(specs: list[dict]) -> list[dict]:
    """The `evaluators` of an experiment create body, from the preflight's evaluator specs."""
    return [_without_nulls({
        "evaluator": spec["evaluator"], "version": spec["version"], "params": spec["params"],
        "connection_id": spec["connection_id"],
    }) for spec in specs]


def _evaluator_keys(evaluators: list[dict]) -> list[str]:
    fields = ("evaluator", "version", "params", "connection_id")
    return sorted(loop_rules.compact({field: item.get(field) or ({} if field == "params" else None) for field in fields})
                  for item in evaluators)


class KitaruExecutor:
    def __init__(self, api: KitaruApi):
        self._api = api

    async def ensure_experiment(self, spec: ExperimentSpec) -> str:
        if spec.experiment_id is not None:
            try:
                experiment = await self._api.get_experiment(spec.experiment_id)
            except KitaruNotFound:
                raise ExecutionFailed(f"The Kitaru experiment {spec.name} no longer exists.") from None
        else:
            # After a restart the id may not be recorded although the create went through.
            experiment = await self._api.find_experiment(spec.agent_id, spec.name)
            if experiment is None:
                experiment = await self._create(spec)
        self._check(spec, experiment)
        return experiment["id"]

    async def _create(self, spec: ExperimentSpec) -> dict:
        body = {
            "name": spec.name, "agent_id": spec.agent_id, "description": spec.description,
            "tool_policy": spec.tool_policy, "evaluators": evaluator_configs(spec.evaluators),
        }
        if spec.override:
            body["override"] = spec.override
        try:
            return await self._api.create_experiment(body, spec.idempotency_key)
        except KitaruConflict:
            # A 409 that is not "still in progress" means the name exists. Read it back.
            experiment = await self._api.find_experiment(spec.agent_id, spec.name)
            if experiment is None:
                raise KitaruTransient from None
            return experiment

    @staticmethod
    def _check(spec: ExperimentSpec, experiment: dict) -> None:
        """The backend does not repair an experiment that someone changed. The Loop fails."""
        same = (
            experiment.get("agent_id") == spec.agent_id
            and _without_nulls(experiment.get("override") or {}) == spec.override
            and _without_nulls(experiment.get("tool_policy")) == _without_nulls(spec.tool_policy)
            and _evaluator_keys(experiment.get("evaluators") or []) == _evaluator_keys(evaluator_configs(spec.evaluators))
        )
        if not same:
            raise ExecutionFailed(f"The Kitaru experiment {spec.name} no longer matches the Loop's settings.")

    async def submit(self, experiment_id: str, run: RunSpec, known_run_ids: set[str]) -> str:
        # Always look first: the submit may have gone through after its key expired.
        earliest = run.submit_started_at - ADOPTION_WINDOW
        candidates = [
            item for item in await self._api.list_runs(experiment_id)
            if item["id"] not in known_run_ids
            and item["cohort_version_id"] == run.cohort_version_id and item["agent_version_id"] == run.agent_version_id
            and datetime.fromisoformat(item["created"]) >= earliest
        ]
        if candidates:
            return min(candidates, key=lambda item: (datetime.fromisoformat(item["created"]), item["id"]))["id"]
        body = {"cohort_version_id": run.cohort_version_id, "agent_version_id": run.agent_version_id,
                "evaluate_baselines": False, "baseline_evaluation_mode": "none"}
        started = await self._api.start_run(experiment_id, body, run.idempotency_key)
        return started["id"]

    async def poll(self, run_id: str) -> dict:
        try:
            return await self._api.get_run(run_id)
        except KitaruNotFound:
            raise RunGone from None

    async def fetch_results(self, run_id: str) -> RunEvidence:
        replays = await self._api.list_replays(run_id)
        evaluations = await self._api.list_evaluations(run_id)
        sessions = {session["id"]: session for session in await self._api.list_result_sessions(run_id)}
        return RunEvidence(replays, evaluations, sessions)

    async def cancel(self, run_id: str) -> None:
        try:
            await self._api.cancel_run(run_id)
        except (KitaruNotFound, KitaruConflict):
            # The run is gone or already finished. Either way nothing is left running.
            pass
