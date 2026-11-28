"""The Loop steps: the backend moves a Loop only when the Loop owner calls it.

There is no background task and no Kitaru key of the backend's own. Each call holds the Loop's lock,
does at most one step with the caller's credential, and writes each fact to the store before the Kitaru
call that depends on it. The rules come from `loop_rules`; this module applies them.

A drive (`advance`) starts a Run. A refresh records a finished Run or an end state and starts no Run.

With the executor `fargate`, a Loop task runs the Runs. The Loop steps get the executor from `create_app`;
the story sc-283 gives them the setting `EVALVE_EXECUTOR`. A drive registers one Kitaru worker for
each job of a Run and stores it, and the Loop task reads the workers as its Assignments. The Run has a
deadline, and the call that collects a Run deletes its workers. A worker token is never logged.
Before it stores a green or red result, the collect checks that the workers of the Run ran each task
that gave the Run its score (the `worker_id` check). A Run that fails the check is `incomplete`.
"""

from __future__ import annotations

import copy
import logging
from asyncio import to_thread
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from evalve_backend import loop_rules
from evalve_backend.auth import Account, Credential
from evalve_backend.executor import (
    START_SETTLES_WITHIN, AmbiguousRun, ExecutionFailed, ExperimentSpec, KitaruExecutor, RunGone, RunSpec,
)
from evalve_backend.kitaru_api import KitaruApi, KitaruNotFound, KitaruRejected, KitaruTransient
from evalve_backend.loop_steps import PreparedStart, StepUnavailable
from evalve_backend.preflight import preflight, run_spec_hash
from evalve_backend.settings import Executor
from evalve_backend.store import RunPlan, RunWorker, Store

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled"}
# The terminal statuses that a run reaches by itself.
FINISHED_RUN_STATUSES = TERMINAL_RUN_STATUSES - {"canceled"}
# The `worker_id` check reads a task with one of these kinds and the status `completed`.
CHECKED_TASK_KINDS = {"agent", "evaluator"}
FOREIGN_TASKS_REASON = (
    "A worker that does not belong to this Loop ran {count} tasks of this Run. While a Loop runs, "
    "stop each worker that has an `agent` claim, an `evaluator` claim or no claim."
)


def rules_state(loop: dict) -> dict:
    """The stored Loop in the shape that `loop_rules` reads."""
    checked = loop["preflight"]
    return {
        **loop, "requested_by": checked["requested_by"], "control_model": checked.get("control_model"),
        "control_prompt": checked.get("control_prompt"), "members": checked["members"],
        "evaluators": checked["evaluator_specs"],
    }


class KitaruLoopSteps:
    """The LoopSteps of the v1 API. The Runs are Kitaru experiment runs, on the executor of the deployment."""

    def __init__(self, kitaru: httpx.AsyncClient, store: Store, kitaru_url: str,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), *,
                 executor: Executor = "kitaru"):
        self._kitaru = kitaru
        self._store = store
        self._kitaru_url = kitaru_url
        self._clock = clock
        self._executor = executor

    async def preflight(self, config: dict, account: Account, credential: Credential) -> PreparedStart:
        try:
            return await preflight(KitaruApi(self._kitaru, credential), config, account, self._kitaru_url)
        except KitaruTransient:
            raise StepUnavailable from None

    async def submit_pending(self, owner: str, loop_id: str, experiment: int, credential: Credential) -> None:
        await self._locked(owner, loop_id, credential, lambda step: step.submit_pending(experiment))

    async def advance(self, owner: str, loop_id: str, credential: Credential) -> None:
        await self._locked(owner, loop_id, credential, lambda step: step.advance())

    async def refresh(self, owner: str, loop_id: str, credential: Credential) -> None:
        await self._locked(owner, loop_id, credential, lambda step: step.refresh())

    async def cancel(self, owner: str, loop_id: str, credential: Credential) -> None:
        await self._locked(owner, loop_id, credential, lambda step: step.cancel())

    async def delete_workers(self, owner: str, loop_id: str, credential: Credential) -> None:
        if self._executor == "fargate":
            await self._locked(owner, loop_id, credential, lambda step: step.delete_leftover_workers())

    async def _locked(self, owner: str, loop_id: str, credential: Credential,
                      action: Callable[[_Step], Awaitable[None]]) -> None:
        # A second call does not wait. A status call shows the stored state, and a start, change or
        # cancel answers 503 because its Run is not confirmed yet.
        lock = await to_thread(self._store.lock_loop, loop_id)
        if lock is None:
            return
        try:
            api = KitaruApi(self._kitaru, credential)
            step = _Step(self._store, api, KitaruExecutor(api), owner, loop_id, self._clock(),
                         loop_task_workers=self._executor == "fargate")
            try:
                await action(step)
            except KitaruTransient:
                raise StepUnavailable from None
            except ExecutionFailed as error:
                await step.end("error", str(error))
            except KitaruRejected as error:
                await step.end("error", f"Kitaru refused a request of the Loop ({error.describe()})")
        finally:
            await to_thread(lock.release)


class _Step:
    """One locked call on one Loop. `loop` is reloaded from the store after each write."""

    def __init__(self, store: Store, api: KitaruApi, executor: KitaruExecutor, owner: str, loop_id: str, now: datetime,
                 *, loop_task_workers: bool = False):
        self.store, self.api, self.executor = store, api, executor
        self.owner, self.loop_id, self.now = owner, loop_id, now
        # True when a Loop task runs the Runs, so each job of a Run needs a worker from the backend.
        self.loop_task_workers = loop_task_workers
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

    # The four operations.

    async def submit_pending(self, number: int) -> None:
        if await self.load():
            await self.submit(number)

    async def refresh(self) -> None:
        """Record a finished Run and apply an end state. It starts no Run."""
        await self.settle()

    async def settle(self) -> dict | None:
        """The part that a refresh and a drive share: collect a finished Run and apply an end state.

        Returns the decision that is left for a drive: `collect` while the Run is still open. Returns
        None when the Loop has nothing more to do now.
        """
        if not await self.load():
            return None
        # A worker that an earlier call could not delete.
        await self.delete_workers()
        decision = self.decide()
        if decision["action"] == "collect":
            # Ask Kitaru one time. A Run that is still going is collected by a later call.
            if not await self.collect():
                return decision
            if not await self.load():
                return None
            decision = self.decide()
        if decision["action"].startswith("stop_"):
            await self.end(decision["action"], decision.get("reason"), decision.get("experiment"))
            return None
        return decision

    async def advance(self) -> None:
        """Drive the Loop: do what a refresh does, then start the Run that the rules ask for."""
        decision = await self.settle()
        if decision is None:
            return
        if decision["action"] == "collect":
            # An earlier drive can have failed before each job of the open Run had a worker.
            experiment = self.loop["experiments"][-1]
            await self.register_workers(experiment["number"], experiment["runs"][-1])
        elif decision["action"] == "resume_start":
            await self.submit(decision["experiment"])
        elif decision["action"] in ("replay", "retry"):
            await self.next_run()

    async def delete_leftover_workers(self) -> None:
        """For a Loop that ended or that waits for a change: a worker that an earlier call could not delete."""
        await self.load()
        if self.loop:
            await self.delete_workers()

    async def cancel(self) -> None:
        if not await self.load():
            return
        experiment = self.loop["experiments"][-1]
        run = experiment["runs"][-1] if experiment["runs"] else None
        if run is not None and run["result"] is None:
            run_id = run["run_id"]
            if run_id is None and experiment["experiment_id"] is not None:
                # The submit may have started a run whose reply was lost. A cancel only looks: sending the
                # submit again could start a run that skipped the checks.
                try:
                    run_id = await self.executor.adopt(experiment["experiment_id"], self.run_spec(experiment, run),
                                                       self.known_run_ids())
                except AmbiguousRun:
                    # Never cancel a run that may be someone else's.
                    run_id = None
                last_sent = run["start_sent_at"] or run["submit_started_at"]
                if run_id is None and self.now - last_sent < START_SETTLES_WITHIN:
                    # A start that timed out, the first or a retry, can still create its run, so one empty
                    # look proves nothing. The Loop keeps running and the route asks the caller to try again.
                    return
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
        # A cancel waits for this request to settle, so record it before it can be sent.
        await to_thread(self.store.record_start_sent, self.owner, self.loop_id, number, run["number"], self.now)
        run_id = await self.executor.submit(experiment_id, self.run_spec(experiment, run), self.known_run_ids(), self.now)
        if await to_thread(self.store.record_run_id, self.owner, self.loop_id, number, run["number"], run_id):
            await self.register_workers(number, run | {"run_id": run_id})

    # The workers of a Run. Only a drive registers them.

    async def register_workers(self, experiment: int, run: dict) -> None:
        """Register one worker for each job of the open Run that has none, and store each one at once.

        A failure leaves the workers that are stored, and the next drive registers the others. A worker
        that Kitaru registered and that the store did not get stays in Kitaru with a token that nobody
        holds. It claims nothing (a known limit).
        """
        if not self.loop_task_workers:
            return
        run_key = (self.owner, self.loop_id, experiment, run["number"])
        with_worker = {worker["job_id"] for worker in await to_thread(self.store.run_workers, *run_key)}
        # Kitaru makes one job for each session of the cohort version, so this Run has each worker.
        if len(with_worker) >= len(self.loop["preflight"]["members"]):
            return
        try:
            jobs = await self.api.list_run_jobs(run["run_id"])
        except KitaruNotFound:
            # The run is gone. The next call records the Run as incomplete.
            return
        for job in jobs:
            if job["id"] in with_worker:
                continue
            registered = await self.api.register_worker(f"evalve-{self.loop_id}-job-{job['id']}", job["id"])
            worker = RunWorker(job["id"], registered["worker"]["id"], registered["token"],
                               datetime.fromisoformat(registered["token_expires_at"]))
            if not await to_thread(self.store.add_run_worker, *run_key, worker):
                # The Loop ended or the Run was collected, so the Run needs no worker.
                return

    async def delete_workers(self) -> None:
        """Delete from Kitaru each worker that no Run needs. A failure never fails the call.

        The row of a worker that Kitaru could not delete stays, and each later call tries again.
        """
        if not self.loop_task_workers:
            return
        for worker in await to_thread(self.store.workers_to_delete, self.owner, self.loop_id):
            try:
                await self.api.delete_worker(worker["worker_id"])
            except KitaruNotFound:
                # The worker is already gone.
                pass
            except KitaruTransient:
                logger.warning("Kitaru could not delete the workers of Loop %s now.", self.loop_id)
                return
            except KitaruRejected as error:
                logger.warning("Kitaru refused to delete a worker of Loop %s (HTTP %d).", self.loop_id, error.status)
                continue
            await to_thread(self.store.record_worker_deleted, self.owner, self.loop_id, worker["experiment_number"],
                            worker["run_number"], worker["job_id"], self.now)

    async def stop_open_run(self) -> None:
        """Cancel the Run that an ending Loop leaves open. No worker claims its tasks after the end,
        and a pending task has no timeout in Kitaru.

        A temporary error stops the call before the Loop ends. The Loop stays running, so a later call
        comes to the same end and cancels again. Nothing after the end would try the cancel again.
        """
        if not self.loop_task_workers:
            return
        runs = self.loop["experiments"][-1]["runs"]
        if not runs or runs[-1]["run_id"] is None or runs[-1]["result"] is not None:
            return
        try:
            await self.executor.cancel(runs[-1]["run_id"])
        except KitaruTransient:
            raise StepUnavailable from None
        except KitaruRejected as error:
            # A later cancel gets the same answer, so the Loop ends.
            logger.warning("Kitaru refused to cancel the open Run of Loop %s (HTTP %d).", self.loop_id, error.status)

    def past_deadline(self, run: dict) -> bool:
        return self.loop_task_workers and run["deadline_at"] is not None and self.now > run["deadline_at"]

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
        """Score the Run in flight if it ended, or if its deadline is past. False while it is still open.

        The order matters: score, do the `worker_id` check, store the result and erase the worker
        tokens, then delete the workers. Kitaru sets the `worker_id` of a task to null when its worker
        is deleted. A check that Kitaru cannot answer stops the call before the result is stored, so the
        next call collects the Run again.
        """
        experiment = self.loop["experiments"][-1]
        run = experiment["runs"][-1]
        state = self.state()
        try:
            kitaru_run = await self.executor.poll(run["run_id"])
            stopped_at_deadline = False
            if kitaru_run["status"] not in TERMINAL_RUN_STATUSES:
                if not self.past_deadline(run):
                    return False
                # A pending task has no timeout in Kitaru, so only the backend ends such a Run.
                await self.executor.cancel(run["run_id"])
                # The run can have finished before the cancel. Then it gets its score. If not, the
                # cancel is the cause, also while Kitaru still shows the run as running.
                kitaru_run = await self.executor.poll(run["run_id"])
                stopped_at_deadline = kitaru_run["status"] not in FINISHED_RUN_STATUSES
            evidence = await self.executor.fetch_results(run["run_id"])
        except RunGone:
            # A recorded run that is gone is an incomplete Run. It counts toward two in a row.
            result = self.missing_result(run)
        else:
            result = loop_rules.score_run(state, kitaru_run, evidence.replays, evidence.evaluations, evidence.sessions)
            if stopped_at_deadline:
                # The run is not completed, so the score is already `incomplete`.
                result["run_error"] = self.deadline_reason(run)
        # A Run that is already incomplete gets no check and no extra call.
        if self.loop_task_workers and result["outcome"] in ("green", "red"):
            foreign = await self.foreign_tasks(experiment["number"], run)
            if foreign is None:
                result = self.missing_result(run)
            elif foreign:
                result |= {"outcome": "incomplete", "run_error": FOREIGN_TASKS_REASON.format(count=foreign),
                           "foreign_tasks": foreign}
        after = copy.deepcopy(state)
        after["experiments"][-1]["runs"][-1]["result"] = result
        needs_change = loop_rules.decide(self.config, after)["action"] == "needs_change"
        collected = await to_thread(self.store.collect_run, self.owner, self.loop_id, experiment["number"], run["number"],
                                    result, self.now, needs_change=needs_change)
        if collected:
            await self.delete_workers()
            if await self.load():
                await self.publish()
        return collected

    async def foreign_tasks(self, experiment: int, run: dict) -> int | None:
        """The `worker_id` check: the number of tasks of the Run that a worker outside the Loop ran.

        The check reads each task with the kind `agent` or `evaluator` and the status `completed`. Such
        a task passes when its worker is the worker that the store holds for its job. The jobs come
        from Kitaru, because a job has no row when a registration stopped before its end. A task shows
        the worker of its last attempt only (an accepted limit). None when the run is gone.
        """
        rows = await to_thread(self.store.run_workers, self.owner, self.loop_id, experiment, run["number"])
        worker_of = {row["job_id"]: row["worker_id"] for row in rows}
        try:
            jobs = await self.api.list_run_jobs(run["run_id"])
        except KitaruNotFound:
            return None
        foreign = 0
        for job in jobs:
            for task in await self.api.list_tasks(job["id"]):
                if task["kind"] not in CHECKED_TASK_KINDS or task["status"] != "completed":
                    continue
                worker_id = task.get("worker_id")
                if not worker_id or worker_id != worker_of.get(job["id"]):
                    foreign += 1
        return foreign

    @staticmethod
    def deadline_reason(run: dict) -> str:
        minutes = round((run["deadline_at"] - (run["start_sent_at"] or run["submit_started_at"])).total_seconds() / 60)
        return f"The Run passed its deadline of {max(minutes, 1)} minutes."

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
        # The store can hold a run ID that this call recorded after it read the Loop.
        if not await self.load():
            return
        await self.stop_open_run()
        ended = await to_thread(self.store.transition, self.owner, self.loop_id, ("running",),
                                ending["status"], ending["outcome"], reason)
        if ended:
            await self.load()
            # No Run of an ended Loop is open, so each worker that is left goes.
            await self.delete_workers()
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
