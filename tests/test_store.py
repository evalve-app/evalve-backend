"""Store contract tests and PostgreSQL migration and concurrency checks."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import CheckViolation

from evalve_backend import database, loop_rules, migrations
from evalve_backend.__main__ import main
from evalve_backend.store import LoopStart, PostgresStore, RunPlan, StartResult
from helpers import create_database, settings
from memory_store import MemoryStore

OWNER = "account-1"
STARTED = datetime(2026, 11, 17, 20, tzinfo=timezone.utc)
COLLECTED = STARTED + timedelta(minutes=2)
PLAN = RunPlan(Decimal("0.03125"), "cohort-v1", "agent-v1", STARTED)
START = LoopStart(
    OWNER, "Triage", "triage", "agent-1",
    {"required_consecutive_greens": 2, "max_runs": 10, "budget_usd": "1.00"},
    {"agent_version_id": PLAN.agent_version_id, "control_estimate_usd": "0.03125"},
    "start-1", "request-hash",
)
RED = {
    "run_id": "kitaru-run-1", "outcome": "red", "recorded_cost_usd": "0.02",
    "cost_incomplete": False, "rows": [{"case": "case-1", "passed": False}],
}
GREEN = {**RED, "outcome": "green", "rows": [{"case": "case-1", "passed": True}]}


def race(*actions):
    ready = Barrier(len(actions), timeout=10)

    def run(action):
        ready.wait()
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


class StoreContract:
    def start(self):
        created = self.store.start_loop(START, PLAN)
        self.assertEqual(created.disposition, "created")
        return created.loop_id

    def collect_control(self, loop_id, result=RED, *, needs_change=True):
        self.assertTrue(self.store.record_experiment_id(OWNER, loop_id, 0, "kitaru-experiment-0"))
        self.assertFalse(self.store.record_experiment_id(OWNER, loop_id, 0, "different-experiment"))
        self.assertTrue(self.store.record_run_id(OWNER, loop_id, 0, 1, "kitaru-run-1"))
        self.assertTrue(self.store.record_run_id(OWNER, loop_id, 0, 1, "kitaru-run-1"))
        self.assertFalse(self.store.record_run_id(OWNER, loop_id, 0, 1, "different-run"))
        wrong_run = {**result, "run_id": "other-run"}
        self.assertFalse(self.store.collect_run(OWNER, loop_id, 0, 1, wrong_run, COLLECTED, needs_change=needs_change))
        self.assertTrue(self.store.collect_run(OWNER, loop_id, 0, 1, result, COLLECTED, needs_change=needs_change))

    def test_store_checkpoints_are_readable_by_loop_rules(self):
        loop_id = self.start()
        loop = self.store.get_loop(OWNER, loop_id)
        self.assertEqual((loop["status"], loop["outcome"], loop["config"], loop["preflight"]),
                         ("running", None, START.config, START.preflight))
        self.assertEqual(loop["experiments"][0]["name"], f"evalve-{loop_id}-exp-0")
        run = self.store.get_run(OWNER, loop_id, 0, 1)
        self.assertIsNone(run["run_id"])
        self.assertEqual((run["cohort_version_id"], run["agent_version_id"], run["submit_started_at"],
                          run["estimated_cost_usd"]),
                         (PLAN.cohort_version_id, PLAN.agent_version_id, STARTED, PLAN.estimated_cost_usd))
        self.assertEqual(loop_rules.decide(loop["config"], loop)["action"], "resume_start")
        self.assertFalse(self.store.add_experiment(OWNER, loop_id, 1, {}, "Too early", PLAN))

        self.collect_control(loop_id)
        loop = self.store.get_loop(OWNER, loop_id)
        self.assertEqual(loop["experiments"][0]["experiment_id"], "kitaru-experiment-0")
        self.assertEqual(loop["waiting_since"], COLLECTED)
        self.assertEqual(loop_rules.decide(loop["config"], loop, COLLECTED, loop["waiting_since"])["action"],
                         "needs_change")
        run = self.store.get_run(OWNER, loop_id, 0, 1)
        self.assertEqual(run["evidence"], RED["rows"])
        self.assertEqual(run["collected_at"], COLLECTED)
        self.assertFalse(self.store.collect_run(OWNER, loop_id, 0, 1, GREEN, COLLECTED + timedelta(hours=1),
                                               needs_change=False))
        self.assertEqual(self.store.get_loop(OWNER, loop_id)["waiting_since"], COLLECTED)

        override = {"system_prompt": "Ask one question."}
        self.assertFalse(self.store.add_experiment(OWNER, loop_id, 2, override, "Wrong number", PLAN))
        self.assertTrue(self.store.add_experiment(OWNER, loop_id, 1, override, "One question", PLAN))
        self.assertFalse(self.store.add_experiment(OWNER, loop_id, 1, override, "Repeated", PLAN))
        loop = self.store.get_loop(OWNER, loop_id)
        self.assertIsNone(loop["waiting_since"])
        self.assertEqual([(item["number"], item["override"]) for item in loop["experiments"]],
                         [(0, {}), (1, override)])
        self.assertEqual(loop["experiments"][1]["runs"][0]["number"], 1)
        self.assertEqual(loop_rules.decide(loop["config"], loop)["action"], "resume_start")

    def test_save_a_second_run_in_order(self):
        loop_id = self.start()
        self.assertFalse(self.store.add_run(OWNER, loop_id, 0, 2, PLAN))
        self.collect_control(loop_id, GREEN, needs_change=False)
        self.assertFalse(self.store.add_run(OWNER, loop_id, 0, 3, PLAN))
        self.assertTrue(self.store.add_run(OWNER, loop_id, 0, 2, replace(PLAN, submit_started_at=COLLECTED)))
        loop = self.store.get_loop(OWNER, loop_id)
        self.assertEqual([run["number"] for run in loop["experiments"][0]["runs"]], [1, 2])
        self.assertEqual(loop_rules.decide(loop["config"], loop)["action"], "resume_start")

    def test_start_idempotency_and_active_slug(self):
        self.assertIsNone(self.store.find_start(OWNER, START.idempotency_key, START.request_hash))
        loop_id = self.start()
        self.assertEqual(self.store.find_start(OWNER, START.idempotency_key, START.request_hash),
                         StartResult("replayed", loop_id))
        self.assertEqual(self.store.start_loop(START, PLAN), StartResult("replayed", loop_id))
        self.assertEqual(self.store.start_loop(replace(START, request_hash="changed"), PLAN),
                         StartResult("key_conflict"))
        next_start = replace(START, idempotency_key="start-2")
        self.assertEqual(self.store.start_loop(next_start, PLAN), StartResult("slug_conflict"))
        self.assertTrue(self.store.transition(OWNER, loop_id, ("running",), "completed", "max_runs"))
        self.assertEqual(self.store.start_loop(next_start, PLAN).disposition, "created")
        self.assertEqual(self.store.start_loop(START, PLAN), StartResult("replayed", loop_id))

    def test_reads_and_writes_scope_to_owner(self):
        loop_id = self.start()
        outsider = "account-2"
        other = self.store.start_loop(replace(START, owner_account_id=outsider, agent_id="different-agent"), PLAN)
        self.assertEqual(other.disposition, "created")
        self.assertIsNone(self.store.get_loop(outsider, loop_id))
        self.assertIsNone(self.store.get_run(outsider, loop_id, 0, 1))
        self.assertFalse(self.store.record_run_id(outsider, loop_id, 0, 1, "run"))
        self.assertFalse(self.store.transition(outsider, loop_id, ("running",), "failed", "error"))
        self.assertEqual([item["loop_id"] for item in self.store.list_loops(outsider)], [other.loop_id])
        self.assertEqual([item["loop_id"] for item in self.store.list_loops(OWNER, status="running")], [loop_id])
        self.assertEqual(self.store.list_loops(OWNER, status="completed"), [])
        self.assertEqual({item["loop_id"] for item in self.store.list_loops(None)}, {loop_id, other.loop_id})
        # The agent filter, alone and with the other filters.
        self.assertEqual([item["loop_id"] for item in self.store.list_loops(None, agent_id="different-agent")], [other.loop_id])
        self.assertEqual([item["loop_id"] for item in self.store.list_loops(None, agent_id=START.agent_id)], [loop_id])
        self.assertEqual(self.store.list_loops(OWNER, agent_id="different-agent"), [])
        self.assertEqual(self.store.list_loops(None, agent_id=START.agent_id, status="completed"), [])
        self.assertEqual(self.store.list_loops(None, agent_id="no-such-agent"), [])
        # A list item has the same form as one Loop, with its Experiments and Runs.
        self.assertEqual(self.store.list_loops(OWNER), [self.store.get_loop(OWNER, loop_id)])
        self.assertEqual(self.store.list_loops(outsider), [self.store.get_loop(outsider, other.loop_id)])
        self.assertEqual(self.store.get_loop(None, loop_id)["owner_account_id"], OWNER)
        self.assertEqual(self.store.get_run(None, loop_id, 0, 1), self.store.get_run(OWNER, loop_id, 0, 1))

    def test_idle_expiry_cannot_overwrite_a_committed_proposal(self):
        loop_id = self.start()
        self.collect_control(loop_id)
        self.assertFalse(self.store.expire_waiting_loop("account-2", loop_id, COLLECTED, "Expired"))
        self.assertTrue(self.store.add_experiment(OWNER, loop_id, 1, {"model": "new"}, "Try a model", PLAN,
                                                  control_model="resolved-control"))
        self.assertFalse(self.store.expire_waiting_loop(OWNER, loop_id, COLLECTED, "Expired"))
        loop = self.store.get_loop(None, loop_id)
        self.assertEqual(loop["status"], "running")
        self.assertEqual(loop["preflight"]["control_model"], "resolved-control")

    def test_a_loop_lock_is_held_by_one_caller_at_a_time(self):
        loop_id = self.start()
        held = self.store.lock_loop(loop_id)
        self.assertIsNotNone(held)
        self.assertIsNone(self.store.lock_loop(loop_id))
        other = self.store.lock_loop("another-loop")
        self.assertIsNotNone(other)
        other.release()
        held.release()
        again = self.store.lock_loop(loop_id)
        self.assertIsNotNone(again)
        again.release()

    def test_racing_lock_attempts_let_one_through(self):
        loop_id = self.start()
        locks = race(*[lambda: self.store.lock_loop(loop_id)] * 4)
        taken = [lock for lock in locks if lock is not None]
        self.assertEqual(len(taken), 1)
        taken[0].release()

    def test_compare_and_set_honors_expected_status_and_terminal_pairs(self):
        loop_id = self.start()
        for status, outcome in (("completed", "not-an-outcome"), ("completed", "error"), ("failed", None)):
            with self.subTest(status=status, outcome=outcome), self.assertRaises(ValueError):
                self.store.transition(OWNER, loop_id, ("running",), status, outcome)
        self.assertFalse(self.store.transition(OWNER, loop_id, ("failed",), "completed", "passed"))
        self.assertTrue(self.store.transition(OWNER, loop_id, ("running",), "cancelled", "stopped_by_user", "User"))
        self.assertFalse(self.store.transition(OWNER, loop_id, ("running",), "completed", "passed"))
        self.assertFalse(self.store.transition(OWNER, loop_id, ("cancelled",), "completed", "passed"))
        final = self.store.get_loop(OWNER, loop_id)
        self.assertEqual((final["status"], final["outcome"], final["reason"]), ("cancelled", "stopped_by_user", "User"))

    def test_the_latest_start_request_is_recorded_until_the_run_has_an_id(self):
        loop_id = self.start()
        self.assertTrue(self.store.record_start_sent(OWNER, loop_id, 0, 1, COLLECTED))
        self.assertEqual(self.store.get_loop(OWNER, loop_id)["experiments"][0]["runs"][0]["start_sent_at"], COLLECTED)
        self.assertTrue(self.store.record_run_id(OWNER, loop_id, 0, 1, "kitaru-run-1"))
        self.assertFalse(self.store.record_start_sent(OWNER, loop_id, 0, 1, COLLECTED))


class MemoryStoreTest(StoreContract, unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()


@unittest.skipUnless(os.environ.get("EVALVE_TEST_DB_HOST"), "set EVALVE_TEST_DB_HOST to test against PostgreSQL")
class PostgresStoreTest(StoreContract, unittest.TestCase):
    def setUp(self):
        self.settings = settings(
            db_host=os.environ["EVALVE_TEST_DB_HOST"],
            db_port=int(os.environ.get("EVALVE_TEST_DB_PORT", "5432")),
            db_user=os.environ.get("EVALVE_TEST_DB_USER", "postgres"),
            db_password=os.environ.get("EVALVE_TEST_DB_PWD", "postgres"),
            db_name=f"evalve_test_{uuid4().hex[:12]}",
            db_ssl_mode="disable", db_ssl_ca=None,
        )
        self.addCleanup(self.drop_database)
        create_database(self.settings)
        migrations.migrate(self.settings)
        self.store = PostgresStore(self.settings)

    def drop_database(self):
        opts = database.connection_options(self.settings, database.MAINTENANCE_DATABASE)
        with psycopg.connect(**opts, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(self.settings.db_name)))

    def connect(self):
        return psycopg.connect(**database.connection_options(self.settings, self.settings.db_name))

    def test_reopened_store_recovers_experiments_and_runs(self):
        loop_id = self.start()
        self.collect_control(loop_id)
        self.assertTrue(self.store.add_experiment(OWNER, loop_id, 1, {"model": "b"}, "Try B", PLAN))
        before = self.store.get_loop(OWNER, loop_id)
        self.assertEqual(migrations.migrate(self.settings), ())
        reopened = PostgresStore(self.settings)
        self.assertEqual(reopened.get_loop(OWNER, loop_id), before)
        self.assertEqual(reopened.get_run(OWNER, loop_id, 0, 1)["evidence"], RED["rows"])
        self.assertIsNone(reopened.get_run(OWNER, loop_id, 1, 1)["run_id"])

    def test_concurrent_starts_return_one_loop_and_one_run(self):
        first, second = race(lambda: self.store.start_loop(START, PLAN), lambda: self.store.start_loop(START, PLAN))
        self.assertCountEqual((first.disposition, second.disposition), ("created", "replayed"))
        self.assertEqual(first.loop_id, second.loop_id)
        self.assertEqual(len(self.store.list_loops(OWNER)), 1)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone(), (1,))

    def test_only_one_concurrent_terminal_transition_wins(self):
        loop_id = self.start()
        complete, cancel = race(
            lambda: self.store.transition(OWNER, loop_id, ("running",), "completed", "passed"),
            lambda: self.store.transition(OWNER, loop_id, ("running",), "cancelled", "stopped_by_user"),
        )
        self.assertNotEqual(complete, cancel)
        final = self.store.get_loop(OWNER, loop_id)
        expected = ("completed", "passed") if complete else ("cancelled", "stopped_by_user")
        self.assertEqual((final["status"], final["outcome"]), expected)

    def test_database_rejects_invalid_pairs_even_without_store(self):
        loop_id = self.start()
        for status, outcome in (("running", "passed"), ("completed", None), ("running-ish", None),
                                ("failed", "passed"), ("completed", "not-an-outcome")):
            with self.subTest(status=status, outcome=outcome):
                with self.assertRaises(CheckViolation), self.connect() as connection:
                    connection.execute("UPDATE loops SET status = %s, outcome = %s WHERE id = %s",
                                       (status, outcome, loop_id))
        pairs = set(loop_rules.END_STATES.values()) | {("completed", "passed"), ("completed", "already_passing")}
        for status, outcome in pairs:
            with self.subTest(status=status, outcome=outcome), self.connect() as connection:
                connection.execute("UPDATE loops SET status = %s, outcome = %s WHERE id = %s",
                                   (status, outcome, loop_id))

    def test_concurrent_candidate_acceptance_commits_one_experiment_and_run(self):
        loop_id = self.start()
        self.collect_control(loop_id)

        def propose(model):
            return self.store.add_experiment(OWNER, loop_id, 1, {"model": model}, f"Try {model}", PLAN)

        first, second = race(lambda: propose("b"), lambda: propose("c"))
        self.assertNotEqual(first, second)
        loop = self.store.get_loop(OWNER, loop_id)
        self.assertIsNone(loop["waiting_since"])
        self.assertEqual([item["number"] for item in loop["experiments"]], [0, 1])
        saved = loop["experiments"][1]
        self.assertEqual(saved["override"], {"model": "b" if first else "c"})
        self.assertEqual([run["number"] for run in saved["runs"]], [1])

    def test_a_kitaru_run_id_cannot_be_recorded_twice(self):
        loop_id = self.start()
        self.collect_control(loop_id, GREEN, needs_change=False)
        self.assertTrue(self.store.add_run(OWNER, loop_id, 0, 2, PLAN))
        self.assertFalse(self.store.record_run_id(OWNER, loop_id, 0, 2, "kitaru-run-1"))
        self.assertIsNone(self.store.get_run(OWNER, loop_id, 0, 2)["run_id"])

    def test_failed_first_run_rolls_back_loop_and_candidate(self):
        invalid = replace(PLAN, estimated_cost_usd=Decimal("-0.10"))
        with self.assertRaises(CheckViolation):
            self.store.start_loop(START, invalid)
        self.assertEqual(self.store.list_loops(OWNER), [])
        self.assertIsNone(self.store.find_start(OWNER, START.idempotency_key, START.request_hash))
        loop_id = self.start()
        self.collect_control(loop_id)
        with self.assertRaises(CheckViolation):
            self.store.add_experiment(OWNER, loop_id, 1, {"model": "b"}, "Try B", invalid)
        waiting = self.store.get_loop(OWNER, loop_id)
        self.assertEqual(waiting["waiting_since"], COLLECTED)
        self.assertEqual([exp["number"] for exp in waiting["experiments"]], [0])

    def test_migrations_serialize_and_preserve_pilot_table(self):
        self.drop_database()
        create_database(self.settings)
        with self.connect() as connection:
            connection.execute("CREATE TABLE evalve_loops (slug text PRIMARY KEY)")
            connection.execute("INSERT INTO evalve_loops VALUES ('pilot')")
        results = race(lambda: migrations.migrate(self.settings), lambda: migrations.migrate(self.settings))
        self.assertCountEqual(results, [(1, 2, 3), ()])
        with self.connect() as connection:
            versions = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual(versions, [(1,), (2,), (3,)])
            self.assertEqual(connection.execute("SELECT slug FROM evalve_loops").fetchone(), ("pilot",))

    def test_upgrade_indexes_existing_loops_across_owners(self):
        self.drop_database()
        create_database(self.settings)
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1]):
            self.assertEqual(migrations.migrate(self.settings), (1,))
        loop_id = self.start()
        other = self.store.start_loop(
            replace(START, owner_account_id="account-2", slug="other"), PLAN,
        )
        self.assertEqual(migrations.migrate(self.settings), (2, 3))
        self.assertEqual(migrations.migrate(self.settings), ())
        with self.connect() as connection:
            index, = connection.execute("SELECT pg_get_indexdef('loops_created'::regclass)").fetchone()
            self.assertIn("USING btree (created_at DESC, id DESC)", index)
        first, = self.store.list_loops(None, limit=1)
        self.assertEqual(first["loop_id"], other.loop_id)
        second, = self.store.list_loops(None, limit=1, before=(first["created_at"], first["loop_id"]))
        self.assertEqual(second["loop_id"], loop_id)

    def test_migration_failure_rolls_back_schema_and_stops_startup(self):
        self.drop_database()
        create_database(self.settings)
        with patch("evalve_backend.__main__.Settings.from_environment", return_value=self.settings):
            next_version = migrations.MIGRATIONS[-1][0] + 1
            broken = migrations.MIGRATIONS + ((next_version, "CREATE TABLE broken"),)
            with (patch.object(migrations, "MIGRATIONS", broken),
                  patch("evalve_backend.__main__.uvicorn.run") as serve,
                  patch("sys.stderr")):
                self.assertEqual(main(), 1)
                serve.assert_not_called()
            with self.connect() as connection:
                self.assertEqual(connection.execute("SELECT to_regclass('schema_migrations')").fetchone(), (None,))
                self.assertEqual(connection.execute("SELECT to_regclass('loops')").fetchone(), (None,))
            with patch("evalve_backend.__main__.uvicorn.run") as serve:
                self.assertEqual(main(), 0)
                serve.assert_called_once()
        self.assertEqual(migrations.migrate(self.settings), ())

    def test_loop_list_cursor_pages_within_owner(self):
        self.start()
        newer = self.store.start_loop(replace(START, slug="newer", idempotency_key="start-2"), PLAN)
        first, = self.store.list_loops(OWNER, limit=1)
        self.assertEqual(first["loop_id"], newer.loop_id)
        second, = self.store.list_loops(OWNER, limit=1, before=(first["created_at"], first["loop_id"]))
        self.assertEqual(second["slug"], START.slug)
