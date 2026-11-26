"""Synchronous Loop storage. Async routes call the store through asyncio.to_thread.

An omitted owner scope (None) permits shared reads. Every write to a Loop requires the Loop owner.
A Loop task has no account: its functions take the Loop ID, and a poll proves itself with the hash
of its secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from evalve_backend import loop_rules
from evalve_backend.database import connection_options
from evalve_backend.settings import Settings


@dataclass(frozen=True)
class LoopStart:
    owner_account_id: str
    name: str
    slug: str
    agent_id: str
    config: dict
    preflight: dict
    idempotency_key: str
    request_hash: str


@dataclass(frozen=True)
class RunPlan:
    estimated_cost_usd: Decimal
    cohort_version_id: str
    agent_version_id: str
    submit_started_at: datetime


@dataclass(frozen=True)
class StartResult:
    disposition: Literal["created", "replayed", "key_conflict", "slug_conflict"]
    loop_id: str | None = None


@dataclass(frozen=True)
class PollResult:
    """What an accepted poll of a Loop task reads: the deadline and the Assignments of the open Run.

    Each Assignment has `run_id` (the Kitaru ID of the Run), `job_id`, `worker_id` and `worker_token`.
    The tokens never go into a repr.
    """

    run_deadline: datetime | None = None
    assignments: tuple[dict, ...] = field(default=(), repr=False)


# The states in which the secret of a Loop task is valid, and the states that end it.
LIVE_TASK_STATES = ("starting", "alive")
ENDED_TASK_STATES = ("exited", "stopped", "failed_start")


def validate_task_end(state: str) -> None:
    """Refuse a state that does not end the secret of a Loop task."""
    if state not in ENDED_TASK_STATES:
        raise ValueError(f"Invalid end state of a Loop task: {state!r}.")


def task_accepts(task: dict | None, loop_status: str | None, secret_hash: str) -> bool:
    """The rule for the secret of a Loop task. `task` is the newest row of the Loop."""
    return (task is not None and loop_status == "running" and task["state"] in LIVE_TASK_STATES
            and hmac.compare_digest(task["secret_hash"], secret_hash))


def run_is_open(run: dict | None, now: datetime) -> bool:
    """Whether the newest Run of a Loop has Assignments to give: a Kitaru ID, no result, and a deadline to come."""
    return (run is not None and run["run_id"] is not None and run["collected_at"] is None
            and run["deadline_at"] is not None and run["deadline_at"] > now)


class LoopLock(Protocol):
    def release(self) -> None: ...


class Store(Protocol):
    def lock_loop(self, loop_id: str) -> LoopLock | None:
        """Take the Loop's step lock without waiting. None when another call holds it."""
        ...

    def find_start(self, owner: str, key: str, request_hash: str) -> StartResult | None: ...
    def start_loop(self, start: LoopStart, first_run: RunPlan) -> StartResult: ...
    def get_loop(self, owner: str | None, loop_id: str) -> dict | None: ...
    def list_loops(self, owner: str | None, *, status: str | None = None, agent_id: str | None = None,
                   limit: int = 50, before: tuple[datetime, str] | None = None) -> list[dict]: ...
    def get_run(self, owner: str | None, loop_id: str, experiment: int, run: int) -> dict | None: ...
    def expire_waiting_loop(self, owner: str, loop_id: str, waiting_since: datetime, reason: str) -> bool: ...
    def add_experiment(self, owner: str, loop_id: str, number: int, override: dict, change: str,
                       first_run: RunPlan, *, control_model: str | None = None) -> bool: ...
    def add_run(self, owner: str, loop_id: str, experiment: int, number: int, plan: RunPlan) -> bool: ...
    def record_experiment_id(self, owner: str, loop_id: str, experiment: int, kitaru_id: str) -> bool: ...
    def record_start_sent(self, owner: str, loop_id: str, experiment: int, run: int, sent_at: datetime) -> bool: ...
    def record_run_id(self, owner: str, loop_id: str, experiment: int, run: int, kitaru_id: str) -> bool: ...
    def collect_run(self, owner: str, loop_id: str, experiment: int, run: int, result: dict,
                    collected_at: datetime, *, needs_change: bool) -> bool: ...
    def transition(self, owner: str, loop_id: str, from_statuses: tuple[str, ...], to_status: str,
                   outcome: str, reason: str | None = None) -> bool: ...

    def add_loop_task(self, owner: str, loop_id: str, secret_hash: str, *,
                      experiment: int | None = None, run: int | None = None) -> int | None:
        """Add the next Loop task of a running Loop, with the Run that is open. Returns its attempt.

        The new row is the newest row, so the secret of each earlier Loop task stops being valid.
        """
        ...

    def end_loop_task(self, loop_id: str, attempt: int, state: str, stop_reason: str | None = None) -> bool:
        """End the secret of a Loop task that is starting or alive."""
        ...

    def latest_loop_task(self, loop_id: str) -> dict | None: ...

    def accepts_loop_task(self, loop_id: str, secret_hash: str) -> bool:
        """Whether a poll with this secret is accepted now. Changes nothing."""
        ...

    def record_poll(self, loop_id: str, secret_hash: str, contract_version: int, polled_at: datetime, *,
                    last: bool = False, refused_reason: str | None = None) -> PollResult | None:
        """Record one poll of a Loop task and read its Assignments. None when the secret is refused.

        `refused_reason` says that the contract version is refused: the Loop task becomes a failed
        start with that reason, and the poll gets no Assignment. `last` ends the secret.
        """
        ...


# Keep the v1 SQL migration frozen when the Loop rules change. Tests exercise the
# allowed pairs against PostgreSQL and the in-memory implementation.
_TERMINAL_PAIRS = frozenset(loop_rules.END_STATES.values()) | {
    ("completed", "passed"), ("completed", "already_passing"),
}


def validate_transition(status: str, outcome: str) -> None:
    if (status, outcome) not in _TERMINAL_PAIRS:
        raise ValueError(f"Invalid terminal Loop state: {status!r}, {outcome!r}.")


def dump_store_json(value) -> str:
    """Encode persisted JSON, keeping amounts exact when callers pass a Decimal."""
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f"{type(item).__name__} is not JSON serializable")

    return json.dumps(value, default=encode, allow_nan=False)


def _jsonb(value) -> Jsonb:
    return Jsonb(value, dumps=dump_store_json)


def _find_start(connection, owner, key, request_hash) -> StartResult | None:
    current = connection.execute(
        "SELECT id, request_hash FROM loops WHERE owner_account_id = %s AND idempotency_key = %s",
        (owner, key),
    ).fetchone()
    if current is None:
        return None
    if current["request_hash"] != request_hash:
        return StartResult("key_conflict")
    return StartResult("replayed", current["id"])


def _lock_running_loop(connection, owner, loop_id) -> dict | None:
    # Every mutation locks the Loop before touching child rows. The caller can
    # commit a checkpoint independently of the Loop-steps advisory lock.
    return connection.execute("""
        SELECT waiting_since FROM loops
        WHERE id = %s AND owner_account_id = %s AND status = 'running'
        FOR UPDATE
    """, (loop_id, owner)).fetchone()


def _insert_experiment(connection, loop_id, number, override, change) -> None:
    connection.execute("""
        INSERT INTO experiments (loop_id, number, name, override, change)
        VALUES (%s, %s, %s, %s, %s)
    """, (loop_id, number, f"evalve-{loop_id}-exp-{number}", _jsonb(override), change))


def _insert_run(connection, loop_id, experiment, number, plan: RunPlan) -> None:
    connection.execute("""
        INSERT INTO runs (loop_id, experiment_number, number, estimated_cost_usd,
                          cohort_version_id, agent_version_id, submit_started_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (loop_id, experiment, number, plan.estimated_cost_usd, plan.cohort_version_id,
          plan.agent_version_id, plan.submit_started_at))


def _newest_task(connection, loop_id, *, lock: bool) -> tuple[dict | None, str | None]:
    """The newest Loop task of a Loop and the status of the Loop.

    A poll that writes locks the Loop first. A new Loop task and the end of the Loop wait for the
    poll, and the poll waits for them, so it never accepts the secret of an older row.
    """
    loop = connection.execute(
        "SELECT status FROM loops WHERE id = %s" + (" FOR SHARE" if lock else ""), (loop_id,),
    ).fetchone()
    if loop is None:
        return None, None
    task = connection.execute("""
        SELECT * FROM loop_tasks WHERE loop_id = %s ORDER BY attempt DESC LIMIT 1
    """ + ("FOR UPDATE" if lock else ""), (loop_id,)).fetchone()
    return task, loop["status"]


def loop_lock_key(loop_id: str) -> int:
    """The one-bigint advisory lock of a Loop. Migrations use the separate two-integer namespace."""
    return int.from_bytes(hashlib.sha256(loop_id.encode()).digest()[:8], "big", signed=True)


class _AdvisoryLock:
    """A session advisory lock, held by its own connection until release."""

    def __init__(self, connection: psycopg.Connection, key: int):
        self._connection = connection
        self._key = key

    def release(self) -> None:
        try:
            self._connection.execute("SELECT pg_advisory_unlock(%s::bigint)", (self._key,))
        finally:
            # Closing the session also frees the lock if the unlock failed.
            self._connection.close()


class PostgresStore:
    def __init__(self, settings: Settings):
        self._options = connection_options(settings, settings.db_name)

    def _connect(self):
        return psycopg.connect(**self._options, row_factory=dict_row)

    def lock_loop(self, loop_id: str) -> LoopLock | None:
        key = loop_lock_key(loop_id)
        connection = psycopg.connect(**self._options, autocommit=True)
        try:
            (taken,) = connection.execute("SELECT pg_try_advisory_lock(%s::bigint)", (key,)).fetchone()
        except BaseException:
            connection.close()
            raise
        if not taken:
            connection.close()
            return None
        return _AdvisoryLock(connection, key)

    def find_start(self, owner: str, key: str, request_hash: str) -> StartResult | None:
        with self._connect() as connection:
            return _find_start(connection, owner, key, request_hash)

    def start_loop(self, start: LoopStart, first_run: RunPlan) -> StartResult:
        with self._connect() as connection:
            existing = _find_start(connection, start.owner_account_id, start.idempotency_key, start.request_hash)
            if existing is not None:
                return existing

            loop_id = str(uuid4())
            inserted = connection.execute("""
                INSERT INTO loops (id, owner_account_id, name, slug, agent_id,
                                   config, preflight, idempotency_key, request_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (loop_id, start.owner_account_id, start.name, start.slug, start.agent_id,
                  _jsonb(start.config), _jsonb(start.preflight), start.idempotency_key, start.request_hash))
            if inserted.fetchone() is None:
                # A concurrent start may have committed while the INSERT waited.
                return _find_start(
                    connection, start.owner_account_id, start.idempotency_key, start.request_hash
                ) or StartResult("slug_conflict")

            _insert_experiment(connection, loop_id, 0, {}, "Unchanged control")
            _insert_run(connection, loop_id, 0, 1, first_run)
            return StartResult("created", loop_id)

    def get_loop(self, owner: str | None, loop_id: str) -> dict | None:
        """Return ordered Experiment and Run summaries from a consistent snapshot."""
        with self._connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            loop = connection.execute("""
                SELECT id AS loop_id, owner_account_id, name, slug, agent_id, status, outcome, reason,
                       config, preflight, pending_change, waiting_since, created_at, updated_at
                FROM loops WHERE (%s::text IS NULL OR owner_account_id = %s) AND id = %s
            """, (owner, owner, loop_id)).fetchone()
            if loop is None:
                return None
            self._add_experiments(connection, [loop])
            return loop

    @staticmethod
    def _add_experiments(connection, loops: list[dict]) -> None:
        """Give each Loop its ordered Experiment and Run summaries, with two queries for all of them."""
        by_loop = {loop["loop_id"]: loop for loop in loops}
        for loop in loops:
            loop["experiments"] = []
        if not loops:
            return
        ids = list(by_loop)
        experiments = {}
        for experiment in connection.execute("""
            SELECT loop_id, number, name, override, change, experiment_id
            FROM experiments WHERE loop_id = ANY(%s) ORDER BY loop_id, number
        """, (ids,)):
            loop_id = experiment.pop("loop_id")
            experiment["runs"] = []
            experiments[loop_id, experiment["number"]] = experiment
            by_loop[loop_id]["experiments"].append(experiment)
        for run in connection.execute("""
            SELECT loop_id, experiment_number, number, estimated_cost_usd, cohort_version_id,
                   agent_version_id, submit_started_at, start_sent_at, run_id, result, collected_at
            FROM runs WHERE loop_id = ANY(%s) ORDER BY loop_id, experiment_number, number
        """, (ids,)):
            experiments[run.pop("loop_id"), run.pop("experiment_number")]["runs"].append(run)

    def list_loops(self, owner: str | None, *, status: str | None = None, agent_id: str | None = None,
                   limit: int = 50, before: tuple[datetime, str] | None = None) -> list[dict]:
        """Return one page of Loops, newest first, each in the same form as `get_loop`.

        The list shows the spend and the next action of each Loop, and both come from the Runs.
        One snapshot and three queries serve the page, however many Loops it has.
        """
        before_time, before_id = before if before is not None else (None, None)
        with self._connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            loops = list(connection.execute("""
                SELECT id AS loop_id, owner_account_id, name, slug, agent_id, status, outcome, reason,
                       config, preflight, pending_change, waiting_since, created_at, updated_at
                FROM loops
                WHERE (%(owner)s::text IS NULL OR owner_account_id = %(owner)s)
                  AND (%(status)s::text IS NULL OR status = %(status)s)
                  AND (%(agent_id)s::text IS NULL OR agent_id = %(agent_id)s)
                  AND (%(before_time)s::timestamptz IS NULL OR (created_at, id) < (%(before_time)s, %(before_id)s))
                ORDER BY created_at DESC, id DESC
                LIMIT %(limit)s
            """, {"owner": owner, "status": status, "agent_id": agent_id, "limit": limit,
                  "before_time": before_time, "before_id": before_id}))
            self._add_experiments(connection, loops)
            return loops

    def get_run(self, owner: str | None, loop_id: str, experiment: int, run: int) -> dict | None:
        """Load a Run with its evidence, including before it has a Kitaru ID."""
        with self._connect() as connection:
            return connection.execute("""
                SELECT runs.* FROM runs JOIN loops ON loops.id = runs.loop_id
                WHERE (%s::text IS NULL OR loops.owner_account_id = %s) AND runs.loop_id = %s
                  AND experiment_number = %s AND number = %s
            """, (owner, owner, loop_id, experiment, run)).fetchone()

    def expire_waiting_loop(self, owner: str, loop_id: str, waiting_since: datetime, reason: str) -> bool:
        """Only expire the wait we observed. A concurrent proposal may have cleared it."""
        status, outcome = loop_rules.END_STATES["stop_idle"]
        with self._connect() as connection:
            return connection.execute("""
                UPDATE loops SET status = %s, outcome = %s, reason = %s,
                                 waiting_since = NULL, pending_change = NULL, updated_at = now()
                WHERE id = %s AND owner_account_id = %s AND status = 'running' AND waiting_since = %s
            """, (status, outcome, reason, loop_id, owner, waiting_since)).rowcount == 1

    def add_experiment(self, owner: str, loop_id: str, number: int, override: dict, change: str,
                       first_run: RunPlan, *, control_model: str | None = None) -> bool:
        with self._connect() as connection:
            loop = _lock_running_loop(connection, owner, loop_id)
            if loop is None or loop["waiting_since"] is None:
                return False
            latest = connection.execute(
                "SELECT max(number) AS number FROM experiments WHERE loop_id = %s", (loop_id,),
            ).fetchone()
            if number != latest["number"] + 1:
                return False
            _insert_experiment(connection, loop_id, number, override, change)
            _insert_run(connection, loop_id, number, 1, first_run)
            connection.execute("""
                UPDATE loops SET waiting_since = NULL, pending_change = NULL, updated_at = now(),
                    preflight = CASE WHEN %s::text IS NULL THEN preflight
                        ELSE jsonb_set(preflight, '{control_model}', to_jsonb(%s::text)) END
                WHERE id = %s
            """, (control_model, control_model, loop_id))
            return True

    def add_run(self, owner: str, loop_id: str, experiment: int, number: int, plan: RunPlan) -> bool:
        with self._connect() as connection:
            loop = _lock_running_loop(connection, owner, loop_id)
            if loop is None or loop["waiting_since"] is not None:
                return False
            latest = connection.execute("""
                SELECT experiment_number, number, collected_at FROM runs WHERE loop_id = %s
                ORDER BY experiment_number DESC, number DESC LIMIT 1
            """, (loop_id,)).fetchone()
            if (latest is None or latest["experiment_number"] != experiment or
                    latest["number"] + 1 != number or latest["collected_at"] is None):
                return False
            _insert_run(connection, loop_id, experiment, number, plan)
            return True

    def record_experiment_id(self, owner: str, loop_id: str, experiment: int, kitaru_id: str) -> bool:
        with self._connect() as connection:
            if _lock_running_loop(connection, owner, loop_id) is None:
                return False
            return connection.execute("""
                UPDATE experiments SET experiment_id = %s
                WHERE loop_id = %s AND number = %s AND (experiment_id IS NULL OR experiment_id = %s)
            """, (kitaru_id, loop_id, experiment, kitaru_id)).rowcount == 1

    def record_start_sent(self, owner: str, loop_id: str, experiment: int, run: int, sent_at: datetime) -> bool:
        with self._connect() as connection:
            if _lock_running_loop(connection, owner, loop_id) is None:
                return False
            return connection.execute("""
                UPDATE runs SET start_sent_at = %s
                WHERE loop_id = %s AND experiment_number = %s AND number = %s AND run_id IS NULL
            """, (sent_at, loop_id, experiment, run)).rowcount == 1

    def record_run_id(self, owner: str, loop_id: str, experiment: int, run: int, kitaru_id: str) -> bool:
        try:
            with self._connect() as connection:
                if _lock_running_loop(connection, owner, loop_id) is None:
                    return False
                return connection.execute("""
                    UPDATE runs SET run_id = %s
                    WHERE loop_id = %s AND experiment_number = %s AND number = %s
                      AND (run_id IS NULL OR run_id = %s)
                """, (kitaru_id, loop_id, experiment, run, kitaru_id)).rowcount == 1
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name != "runs_kitaru_run_id_key":
                raise
            return False

    def collect_run(self, owner: str, loop_id: str, experiment: int, run: int, result: dict,
                    collected_at: datetime, *, needs_change: bool) -> bool:
        with self._connect() as connection:
            if _lock_running_loop(connection, owner, loop_id) is None:
                return False
            summary = dict(result)
            evidence = summary.pop("rows")
            updated = connection.execute("""
                UPDATE runs SET result = %s, evidence = %s, collected_at = %s
                WHERE loop_id = %s AND experiment_number = %s AND number = %s
                  AND run_id = %s AND collected_at IS NULL
            """, (_jsonb(summary), _jsonb(evidence), collected_at, loop_id, experiment, run, summary["run_id"])
            ).rowcount == 1
            if updated:
                connection.execute("""
                    UPDATE loops SET waiting_since = %s, updated_at = now() WHERE id = %s
                """, (collected_at if needs_change else None, loop_id))
            return updated

    def transition(self, owner: str, loop_id: str, from_statuses: tuple[str, ...], to_status: str,
                   outcome: str, reason: str | None = None) -> bool:
        """Commit the terminal pair if the Loop is still at an expected status."""
        validate_transition(to_status, outcome)
        with self._connect() as connection:
            return connection.execute("""
                UPDATE loops SET status = %s, outcome = %s, reason = %s,
                                 waiting_since = NULL, pending_change = NULL, updated_at = now()
                WHERE id = %s AND owner_account_id = %s AND status = 'running' AND status = ANY(%s)
            """, (to_status, outcome, reason, loop_id, owner, list(from_statuses))).rowcount == 1

    def add_loop_task(self, owner: str, loop_id: str, secret_hash: str, *,
                      experiment: int | None = None, run: int | None = None) -> int | None:
        with self._connect() as connection:
            # The lock on the Loop gives each Loop task of the Loop a different attempt.
            if _lock_running_loop(connection, owner, loop_id) is None:
                return None
            return connection.execute("""
                INSERT INTO loop_tasks (loop_id, attempt, secret_hash, experiment_number, run_number)
                SELECT %s, coalesce(max(attempt), 0) + 1, %s, %s, %s FROM loop_tasks WHERE loop_id = %s
                RETURNING attempt
            """, (loop_id, secret_hash, experiment, run, loop_id)).fetchone()["attempt"]

    def end_loop_task(self, loop_id: str, attempt: int, state: str, stop_reason: str | None = None) -> bool:
        validate_task_end(state)
        with self._connect() as connection:
            return connection.execute("""
                UPDATE loop_tasks SET state = %s, stop_reason = %s
                WHERE loop_id = %s AND attempt = %s AND state = ANY(%s)
            """, (state, stop_reason, loop_id, attempt, list(LIVE_TASK_STATES))).rowcount == 1

    def latest_loop_task(self, loop_id: str) -> dict | None:
        with self._connect() as connection:
            return _newest_task(connection, loop_id, lock=False)[0]

    def accepts_loop_task(self, loop_id: str, secret_hash: str) -> bool:
        with self._connect() as connection:
            return task_accepts(*_newest_task(connection, loop_id, lock=False), secret_hash)

    def record_poll(self, loop_id: str, secret_hash: str, contract_version: int, polled_at: datetime, *,
                    last: bool = False, refused_reason: str | None = None) -> PollResult | None:
        with self._connect() as connection:
            task, loop_status = _newest_task(connection, loop_id, lock=True)
            if not task_accepts(task, loop_status, secret_hash):
                return None
            key = (loop_id, task["attempt"])
            if refused_reason is not None:
                connection.execute("""
                    UPDATE loop_tasks SET state = 'failed_start', contract_version = %s, stop_reason = %s
                    WHERE loop_id = %s AND attempt = %s
                """, (contract_version, refused_reason, *key))
                return PollResult()
            connection.execute("""
                UPDATE loop_tasks SET state = %s, contract_version = %s, last_poll_at = %s,
                                      first_poll_at = coalesce(first_poll_at, %s)
                WHERE loop_id = %s AND attempt = %s
            """, ("exited" if last else "alive", contract_version, polled_at, polled_at, *key))
            if last:
                return PollResult()
            run = connection.execute("""
                SELECT experiment_number, number, run_id, collected_at, deadline_at FROM runs
                WHERE loop_id = %s ORDER BY experiment_number DESC, number DESC LIMIT 1
            """, (loop_id,)).fetchone()
            if not run_is_open(run, polled_at):
                return PollResult()
            workers = connection.execute("""
                SELECT job_id, worker_id, worker_token FROM run_workers
                WHERE loop_id = %s AND experiment_number = %s AND run_number = %s AND worker_token IS NOT NULL
                ORDER BY job_id
            """, (loop_id, run["experiment_number"], run["number"])).fetchall()
            return PollResult(run["deadline_at"], tuple({"run_id": run["run_id"], **worker} for worker in workers))
