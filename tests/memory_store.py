"""In-memory Store for API and Loop-step tests."""

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from evalve_backend.store import (
    LIVE_TASK_STATES, LoopStart, RunPlan, StartResult, Store, PollResult, dump_store_json, run_is_open,
    task_accepts,
    validate_task_end, validate_transition,
)


class _MemoryLock:
    def __init__(self, lock: threading.Lock):
        self._lock = lock

    def release(self) -> None:
        self._lock.release()


class MemoryStore(Store):
    """Test double with the same state shape and write guards. Writes happen one at a time."""

    def __init__(self):
        self._loops: dict[str, dict] = {}
        self._starts: dict[tuple[str, str], tuple[str, str]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._tasks: dict[str, list[dict]] = {}
        self._workers: dict[tuple[str, int, int], list[dict]] = {}

    def lock_loop(self, loop_id):
        with self._locks_guard:
            lock = self._locks.setdefault(loop_id, threading.Lock())
        return _MemoryLock(lock) if lock.acquire(blocking=False) else None

    def find_start(self, owner, key, request_hash):
        existing = self._starts.get((owner, key))
        if existing is None:
            return None
        loop_id, saved_hash = existing
        if saved_hash != request_hash:
            return StartResult("key_conflict")
        return StartResult("replayed", loop_id)

    def start_loop(self, start: LoopStart, first_run: RunPlan):
        existing = self.find_start(start.owner_account_id, start.idempotency_key, start.request_hash)
        if existing is not None:
            return existing
        if any(loop["status"] == "running" and (loop["agent_id"], loop["slug"]) == (start.agent_id, start.slug)
               for loop in self._loops.values()):
            return StartResult("slug_conflict")
        loop_id = str(uuid4())
        now = datetime.now(timezone.utc)
        self._loops[loop_id] = {
            "loop_id": loop_id, "owner_account_id": start.owner_account_id,
            "name": start.name, "slug": start.slug, "agent_id": start.agent_id,
            "config": json.loads(dump_store_json(start.config)), "preflight": json.loads(dump_store_json(start.preflight)),
            "status": "running", "outcome": None, "reason": None, "pending_change": None, "waiting_since": None,
            "created_at": now, "updated_at": now,
            "experiments": [self._experiment(loop_id, 0, {}, "Unchanged control", first_run)],
        }
        self._starts[start.owner_account_id, start.idempotency_key] = loop_id, start.request_hash
        return StartResult("created", loop_id)

    @staticmethod
    def _experiment(loop_id, number, override, change, first_run):
        return {
            "number": number, "name": f"evalve-{loop_id}-exp-{number}", "override": json.loads(dump_store_json(override)),
            "change": change, "experiment_id": None, "runs": [MemoryStore._run(1, first_run)],
        }

    @staticmethod
    def _run(number, plan):
        return {
            "number": number, "estimated_cost_usd": plan.estimated_cost_usd,
            "cohort_version_id": plan.cohort_version_id, "agent_version_id": plan.agent_version_id,
            "submit_started_at": plan.submit_started_at, "start_sent_at": None, "run_id": None,
            "result": None, "evidence": None, "collected_at": None, "deadline_at": None,
        }

    def _owned_loop(self, owner, loop_id):
        loop = self._loops.get(loop_id)
        return loop if loop and loop["owner_account_id"] == owner else None

    def _running_loop(self, owner, loop_id):
        loop = self._owned_loop(owner, loop_id)
        return loop if loop and loop["status"] == "running" else None

    @staticmethod
    def _find_experiment(loop, experiment):
        return next((item for item in loop["experiments"] if item["number"] == experiment), None) if loop else None

    @staticmethod
    def _find_run(experiment, run):
        return next((item for item in experiment["runs"] if item["number"] == run), None) if experiment else None

    def get_loop(self, owner, loop_id):
        loop = deepcopy(self._loops.get(loop_id) if owner is None else self._owned_loop(owner, loop_id))
        if loop:
            for experiment in loop["experiments"]:
                for run in experiment["runs"]:
                    del run["evidence"], run["deadline_at"]
        return loop

    def list_loops(self, owner, *, status=None, agent_id=None, limit=50, before=None):
        def order(loop):
            return loop["created_at"], loop["loop_id"]

        loops = sorted(self._loops.values(), key=order, reverse=True)
        return [self.get_loop(None, loop["loop_id"]) for loop in loops
                if (owner is None or loop["owner_account_id"] == owner) and (status is None or loop["status"] == status)
                and (agent_id is None or loop["agent_id"] == agent_id)
                and (before is None or order(loop) < before)][:limit]

    def get_run(self, owner, loop_id, experiment, run):
        loop = self._loops.get(loop_id) if owner is None else self._owned_loop(owner, loop_id)
        parent = self._find_experiment(loop, experiment)
        saved = self._find_run(parent, run)
        return deepcopy(saved) | {"loop_id": loop_id, "experiment_number": experiment} if saved else None

    def expire_waiting_loop(self, owner, loop_id, waiting_since, reason):
        loop = self._running_loop(owner, loop_id)
        if loop is None or loop["waiting_since"] != waiting_since:
            return False
        return self.transition(owner, loop_id, ("running",), "completed", "stopped_idle", reason)

    def add_experiment(self, owner, loop_id, number, override, change, first_run, *, control_model=None):
        loop = self._running_loop(owner, loop_id)
        if loop is None or loop["waiting_since"] is None or number != loop["experiments"][-1]["number"] + 1:
            return False
        loop["experiments"].append(self._experiment(loop_id, number, override, change, first_run))
        if control_model is not None:
            loop["preflight"]["control_model"] = control_model
        loop.update(waiting_since=None, pending_change=None, updated_at=datetime.now(timezone.utc))
        return True

    def add_run(self, owner, loop_id, experiment, number, plan):
        loop = self._running_loop(owner, loop_id)
        if loop is None or loop["waiting_since"] is not None:
            return False
        latest = loop["experiments"][-1]
        last_run = latest["runs"][-1]
        if latest["number"] != experiment or last_run["number"] + 1 != number or last_run["collected_at"] is None:
            return False
        latest["runs"].append(self._run(number, plan))
        return True

    def record_experiment_id(self, owner, loop_id, experiment, kitaru_id):
        parent = self._find_experiment(self._running_loop(owner, loop_id), experiment)
        if parent is None or parent["experiment_id"] not in (None, kitaru_id):
            return False
        parent["experiment_id"] = kitaru_id
        return True

    def record_start_sent(self, owner, loop_id, experiment, run, sent_at):
        saved = self._find_run(self._find_experiment(self._running_loop(owner, loop_id), experiment), run)
        if saved is None or saved["run_id"] is not None:
            return False
        saved["start_sent_at"] = sent_at
        return True

    def record_run_id(self, owner, loop_id, experiment, run, kitaru_id):
        saved = self._find_run(self._find_experiment(self._running_loop(owner, loop_id), experiment), run)
        if saved is None or saved["run_id"] not in (None, kitaru_id):
            return False
        if any(other is not saved and other["run_id"] == kitaru_id for loop in self._loops.values()
               for parent in loop["experiments"] for other in parent["runs"]):
            return False
        saved["run_id"] = kitaru_id
        return True

    def collect_run(self, owner, loop_id, experiment, run, result, collected_at, *, needs_change):
        loop = self._running_loop(owner, loop_id)
        saved = self._find_run(self._find_experiment(loop, experiment), run)
        if saved is None or saved["run_id"] is None or saved["run_id"] != result["run_id"] or saved["collected_at"] is not None:
            return False
        summary = json.loads(dump_store_json(result))
        evidence = summary.pop("rows")
        saved.update(result=summary, evidence=evidence, collected_at=collected_at)
        loop.update(waiting_since=collected_at if needs_change else None, updated_at=datetime.now(timezone.utc))
        return True

    def transition(self, owner, loop_id, from_statuses, to_status, outcome, reason=None):
        validate_transition(to_status, outcome)
        loop = self._running_loop(owner, loop_id)
        if loop is None or "running" not in from_statuses:
            return False
        loop.update(status=to_status, outcome=outcome, reason=reason, waiting_since=None,
                    pending_change=None, updated_at=datetime.now(timezone.utc))
        return True

    def seed_assignments(self, loop_id, experiment, run, deadline, workers):
        """Test setup only. The story for the workers of a Run adds the store function for this."""
        saved = self._find_run(self._find_experiment(self._loops[loop_id], experiment), run)
        saved["deadline_at"] = deadline
        self._workers[loop_id, experiment, run] = [
            {"job_id": job_id, "worker_id": worker_id, "worker_token": token} for job_id, worker_id, token in workers
        ]

    def add_loop_task(self, owner, loop_id, secret_hash, *, experiment=None, run=None):
        if self._running_loop(owner, loop_id) is None:
            return None
        tasks = self._tasks.setdefault(loop_id, [])
        tasks.append({
            "loop_id": loop_id, "attempt": len(tasks) + 1, "secret_hash": secret_hash, "state": "starting",
            "task_arn": None, "task_definition_arn": None, "created_at": datetime.now(timezone.utc),
            "first_poll_at": None, "last_poll_at": None, "contract_version": None, "stop_reason": None,
            "experiment_number": experiment, "run_number": run,
        })
        return len(tasks)

    def end_loop_task(self, loop_id, attempt, state, stop_reason=None):
        validate_task_end(state)
        task = next((item for item in self._tasks.get(loop_id, []) if item["attempt"] == attempt), None)
        if task is None or task["state"] not in LIVE_TASK_STATES:
            return False
        task.update(state=state, stop_reason=stop_reason)
        return True

    def _newest_task(self, loop_id):
        tasks = self._tasks.get(loop_id)
        return (tasks[-1], self._loops[loop_id]["status"]) if tasks else (None, None)

    def latest_loop_task(self, loop_id):
        return deepcopy(self._newest_task(loop_id)[0])

    def accepts_loop_task(self, loop_id, secret_hash):
        return task_accepts(*self._newest_task(loop_id), secret_hash)

    def record_poll(self, loop_id, secret_hash, contract_version, polled_at, *, last=False, refused_reason=None):
        task, loop_status = self._newest_task(loop_id)
        if not task_accepts(task, loop_status, secret_hash):
            return None
        if refused_reason is not None:
            task.update(state="failed_start", contract_version=contract_version, stop_reason=refused_reason)
            return PollResult()
        task.update(state="exited" if last else "alive", contract_version=contract_version, last_poll_at=polled_at,
                    first_poll_at=task["first_poll_at"] or polled_at)
        if last:
            return PollResult()
        experiment = self._loops[loop_id]["experiments"][-1]
        run = experiment["runs"][-1]
        if not run_is_open(run, polled_at):
            return PollResult()
        workers = self._workers.get((loop_id, experiment["number"], run["number"]), [])
        return PollResult(run["deadline_at"], tuple(
            {"run_id": run["run_id"], **worker}
            for worker in sorted(workers, key=lambda worker: worker["job_id"])
            if worker["worker_token"] is not None
        ))
