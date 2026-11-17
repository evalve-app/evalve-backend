"""The Loop steps: the backend moves a Loop forward only when the Loop owner's agent calls it.

There is no background task and no Kitaru key of the backend's own. Each call holds the Loop's lock,
does at most one step with the caller's key, and writes each fact to the store before the Kitaru
call that depends on it. The rules come from `loop_rules`; this module applies them.
"""

from __future__ import annotations

import copy
import logging
from asyncio import to_thread
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from evalve_backend import loop_rules
from evalve_backend.auth import Account
from evalve_backend.executor import ExecutionFailed, ExperimentSpec, KitaruExecutor, RunGone, RunSpec
from evalve_backend.kitaru_api import KitaruApi, KitaruNotFound, KitaruRejected, KitaruTransient
from evalve_backend.loop_steps import PreparedStart, StepUnavailable
from evalve_backend.preflight import preflight, run_spec_hash
from evalve_backend.store import RunPlan, Store

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled"}


def rules_state(loop: dict) -> dict:
    """The stored Loop in the shape that `loop_rules` reads."""
    checked = loop["preflight"]
    return {
        **loop, "requested_by": checked["requested_by"], "control_model": checked.get("control_model"),
        "control_prompt": checked.get("control_prompt"), "members": checked["members"],
        "evaluators": checked["evaluator_specs"],
    }


class KitaruLoopSteps:
    """The LoopSteps of the v1 API, with Runs on the Kitaru workers."""

    def __init__(self, kitaru: httpx.AsyncClient, store: Store, kitaru_url: str,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._kitaru = kitaru
        self._store = store
        self._kitaru_url = kitaru_url
        self._clock = clock

    async def preflight(self, config: dict, account: Account, authorization: str) -> PreparedStart:
        try:
            return await preflight(KitaruApi(self._kitaru, authorization), config, account, self._kitaru_url)
        except KitaruTransient:
            raise StepUnavailable from None

    async def submit_pending(self, owner: str, loop_id: str, experiment: int, authorization: str) -> None:
        await self._locked(owner, loop_id, authorization, lambda step: step.submit_pending(experiment))

    async def advance(self, owner: str, loop_id: str, authorization: str) -> None:
        await self._locked(owner, loop_id, authorization, lambda step: step.advance())

    async def cancel(self, owner: str, loop_id: str, authorization: str) -> None:
        await self._locked(owner, loop_id, authorization, lambda step: step.cancel())

    async def _locked(self, owner: str, loop_id: str, authorization: str,
                      action: Callable[[_Step], Awaitable[None]]) -> None:
        # A second call does not wait. A status call shows the stored state, and a start, change or
        # cancel answers 503 because its Run is not confirmed yet.
        lock = await to_thread(self._store.lock_loop, loop_id)
        if lock is None:
            return
        try:
            api = KitaruApi(self._kitaru, authorization)
            step = _Step(self._store, api, KitaruExecutor(api), owner, loop_id, self._clock())
            try:
                await action(step)
            except KitaruTransient:
                raise StepUnavailable from None
            except ExecutionFailed as error:
                await step.end("error", str(error))
            except KitaruRejected as error:
                await step.end("error", f"Kitaru refused a request of the Loop: {error.detail}")
        finally:
            await to_thread(lock.release)


class _Step:
    """One locked call on one Loop. `loop` is reloaded from the store after each write."""

    def __init__(self, store: Store, api: KitaruApi, executor: KitaruExecutor, owner: str, loop_id: str, now: datetime):
        self.store, self.api, self.executor = store, api, executor
        self.owner, self.loop_id, self.now = owner, loop_id, now
        self.loop: dict = {}

    async def load(self) -> bool:
        """Read the Loop. True while it is running."""
        loop = await to_thread(self.store.get_loop, self.owner, self.loop_id)
        self.loop = loop or {}
        return bool(loop) and loop["status"] == "running"

    @property
    def config(self) -> dict:
        return self.loop["config"]

    def state(self) -> dict:
        return rules_state(self.loop)

    def decide(self) -> dict:
        return loop_rules.decide(self.config, self.state(), self.now, self.loop["waiting_since"])

    # The three operations.

    async def submit_pending(self, number: int) -> None:
        if await self.load():
            await self.submit(number)

    async def advance(self) -> None:
        if not await self.load():
            return
        decision = self.decide()
        if decision["action"] == "collect":
            # Ask Kitaru one time. A Run that is still going is collected by a later call.
            if not await self.collect() or not await self.load():
                return
            decision = self.decide()
        action = decision["action"]
        if action == "resume_start":
            await self.submit(decision["experiment"])
        elif action in ("replay", "retry"):
            await self.next_run()
        elif action.startswith("stop_"):
            await self.end(action, decision.get("reason"), decision.get("experiment"))

    async def cancel(self) -> None:
        if not await self.load():
            return
        experiment = self.loop["experiments"][-1]
        run = experiment["runs"][-1] if experiment["runs"] else None
        if run is not None and run["result"] is None:
            run_id = run["run_id"]
            if run_id is None and experiment["experiment_id"] is not None:
                # The submit may have started a run whose reply was lost.
                run_id = await self.executor.adopt(experiment["experiment_id"], self.run_spec(experiment, run), self.known_run_ids())
                if run_id is not None:
                    await to_thread(self.store.record_run_id, self.owner, self.loop_id, experiment["number"],
                                    run["number"], run_id)
            if run_id is not None:
                await self.executor.cancel(run_id)
        await self.end("cancelled", None)

    # Starting Runs.

    async def next_run(self) -> None:
        experiment = self.loop["experiments"][-1]
        estimate = loop_rules.estimate_run(self.state(), experiment["override"])
        if not await self.fits_budget(estimate):
            return
        number = experiment["runs"][-1]["number"] + 1
        plan = RunPlan(estimate, self.config["cohort_version_id"], self.loop["preflight"]["agent_version_id"], self.now)
        # The Run row comes first. It is the record of the submit if the reply is lost.
        if await to_thread(self.store.add_run, self.owner, self.loop_id, experiment["number"], number, plan) and await self.load():
            await self.submit(experiment["number"])

    async def submit(self, number: int) -> None:
        experiment = self.loop["experiments"][number]
        run = experiment["runs"][-1]
        if run["run_id"] is not None or run["result"] is not None:
            return
        if not await self.fits_budget(loop_rules.dollars(run["estimated_cost_usd"])):
            return
        await self.check_run_spec()
        experiment_id = await self.executor.ensure_experiment(self.experiment_spec(experiment))
        await to_thread(self.store.record_experiment_id, self.owner, self.loop_id, number, experiment_id)
        run_id = await self.executor.submit(experiment_id, self.run_spec(experiment, run), self.known_run_ids())
        await to_thread(self.store.record_run_id, self.owner, self.loop_id, number, run["number"], run_id)

    async def fits_budget(self, estimate) -> bool:
        decision = loop_rules.decide(self.config, self.state())
        plan = loop_rules.plan_submission(self.config, self.state(), decision, estimate)
        if "budget_stop" in plan:
            await self.end("stop_budget", plan["budget_stop"])
            return False
        return True

    async def check_run_spec(self) -> None:
        checked = self.loop["preflight"]
        try:
            version = await self.api.get_agent_version(checked["agent_version_id"])
        except KitaruNotFound:
            raise ExecutionFailed("The agent version of the Loop no longer exists in Kitaru.") from None
        if run_spec_hash(version) != checked["run_spec_hash"]:
            raise ExecutionFailed("The agent version's run spec changed after the Loop started.")

    def experiment_spec(self, experiment: dict) -> ExperimentSpec:
        return ExperimentSpec(
            name=experiment["name"], idempotency_key=experiment["name"], agent_id=self.loop["agent_id"],
            description=loop_rules.description(self.config, self.state(), experiment), override=experiment["override"],
            tool_policy=self.config["tool_policy"], evaluators=self.loop["preflight"]["evaluator_specs"],
            experiment_id=experiment["experiment_id"],
        )

    @staticmethod
    def run_spec(experiment: dict, run: dict) -> RunSpec:
        return RunSpec(f"{experiment['name']}-run-{run['number']}", run["cohort_version_id"], run["agent_version_id"],
                       run["submit_started_at"])

    def known_run_ids(self) -> set[str]:
        return {run["run_id"] for experiment in self.loop["experiments"] for run in experiment["runs"] if run["run_id"]}

    # Collecting a finished Run.

    async def collect(self) -> bool:
        """Score the Run in flight if it ended. False while it is still running."""
        experiment = self.loop["experiments"][-1]
        run = experiment["runs"][-1]
        state = self.state()
        try:
            kitaru_run = await self.executor.poll(run["run_id"])
        except RunGone:
            # A recorded run that is gone is an incomplete Run. It counts toward two in a row.
            result = self.missing_result(run)
        else:
            if kitaru_run["status"] not in TERMINAL_RUN_STATUSES:
                return False
            evidence = await self.executor.fetch_results(run["run_id"])
            result = loop_rules.score_run(state, kitaru_run, evidence.replays, evidence.evaluations, evidence.sessions)
        after = copy.deepcopy(state)
        after["experiments"][-1]["runs"][-1]["result"] = result
        needs_change = loop_rules.decide(self.config, after)["action"] == "needs_change"
        collected = await to_thread(self.store.collect_run, self.owner, self.loop_id, experiment["number"], run["number"],
                                    result, self.now, needs_change=needs_change)
        if collected and await self.load():
            await self.publish()
        return collected

    def missing_result(self, run: dict) -> dict:
        members = sorted(self.loop["preflight"]["members"])
        return {
            "run_id": run["run_id"], "run_status": "missing", "run_error": "The Kitaru run no longer exists.",
            "outcome": "incomplete", "passed": 0, "failed": 0, "unscored": len(members), "missing_session_ids": members,
            "recorded_cost_usd": "0", "cost_incomplete": True, "rows": [],
        }

    # Ending and publishing.

    async def end(self, stop: str, reason: str | None, experiment: int | None = None) -> None:
        ending = loop_rules.end_state(stop, experiment)
        ended = await to_thread(self.store.transition, self.owner, self.loop_id, ("running",),
                                ending["status"], ending["outcome"], reason)
        if ended:
            await self.load()
            await self.publish(ended=stop, reason=reason)

    async def publish(self, *, ended: str | None = None, reason: str | None = None) -> None:
        """Write the `Evalve result:` line on the Control Experiment, as `loop.py` does.

        A failed publish does not fail the call. The store holds the result, and the dashboard shows
        the last line that was published (a known gap, noted outside this story).
        """
        control = self.loop["experiments"][0]
        if control["experiment_id"] is None:
            return
        try:
            experiment = await self.api.get_experiment(control["experiment_id"])
            original = experiment.get("description") or ""
            metadata = loop_rules.result_metadata(self.config, self.state(), self.now, self.loop["waiting_since"],
                                                  ended=ended, reason=reason)
            lines = [line for line in original.splitlines() if not line.startswith(loop_rules.RESULT_PREFIX)]
            updated = "\n".join([*lines, loop_rules.RESULT_PREFIX + loop_rules.compact(metadata)])
            if updated != original:
                await self.api.update_description(control["experiment_id"], updated)
        except (KitaruTransient, KitaruRejected) as error:
            logger.warning("Could not publish the result of Loop %s (%s).", self.loop_id, type(error).__name__)
