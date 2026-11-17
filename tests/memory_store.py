"""In-memory Store for API and Loop-step tests."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from evalve_backend.store import LoopStart, RunPlan, StartResult, Store, dump_store_json, validate_transition


class MemoryStore(Store):
    """Single-threaded test double with the same state shape and write guards."""

    def __init__(self):
        self._loops: dict[str, dict] = {}
        self._starts: dict[tuple[str, str], tuple[str, str]] = {}

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
            "submit_started_at": plan.submit_started_at, "run_id": None,
            "result": None, "evidence": None, "collected_at": None,
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
        loop = deepcopy(self._owned_loop(owner, loop_id))
        if loop:
            for experiment in loop["experiments"]:
                for run in experiment["runs"]:
                    del run["evidence"]
        return loop

    def list_loops(self, owner, *, status=None, limit=50, before=None):
        def order(loop):
            return loop["created_at"], loop["loop_id"]

        fields = ("loop_id", "name", "slug", "agent_id", "status", "outcome", "reason", "created_at", "updated_at")
        loops = sorted(self._loops.values(), key=order, reverse=True)
        return [{field: loop[field] for field in fields} for loop in loops
                if loop["owner_account_id"] == owner and (status is None or loop["status"] == status)
                and (before is None or order(loop) < before)][:limit]

    def get_run(self, owner, loop_id, experiment, run):
        parent = self._find_experiment(self._owned_loop(owner, loop_id), experiment)
        saved = self._find_run(parent, run)
        return deepcopy(saved) | {"loop_id": loop_id, "experiment_number": experiment} if saved else None

    def add_experiment(self, owner, loop_id, number, override, change, first_run):
        loop = self._running_loop(owner, loop_id)
        if loop is None or loop["waiting_since"] is None or number != loop["experiments"][-1]["number"] + 1:
            return False
        loop["experiments"].append(self._experiment(loop_id, number, override, change, first_run))
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
