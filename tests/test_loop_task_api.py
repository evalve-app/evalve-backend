"""The route from which a Loop task reads its Assignments, with the secret of the Loop task."""

import logging
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

from evalve_backend.store import LoopStart, RunPlan
from helpers import ACCOUNT, TOKEN, FakeKitaru, client
from memory_store import MemoryStore

LOOPS = "/evalve/v1/loops"
OWNER = ACCOUNT["id"]
SECRET = "loop-task-secret-value"
OLD_SECRET = "older-loop-task-secret-value"
TASK = {"Authorization": f"EvalveLoopTask {SECRET}"}
OLD_TASK = {"Authorization": f"EvalveLoopTask {OLD_SECRET}"}
SESSION = {"Cookie": "evalve_session=session-token-value", "X-CSRF-Token": "csrf-token-value"}
POLL = {"contract_version": 1, "last": False}
PLAN = RunPlan(Decimal("0.20"), "cohort-v1", "agent-v1", datetime(2026, 11, 25, 9, tzinfo=timezone.utc))
START = LoopStart(
    OWNER, "Triage", "triage", "agent-1",
    {"required_consecutive_greens": 2, "max_runs": 10, "budget_usd": "1.00"},
    {"agent_version_id": PLAN.agent_version_id, "control_estimate_usd": "0.20"},
    "start-1", "request-hash",
)
WORKERS = [("job-1", "worker-1", "worker-token-one"), ("job-2", "worker-2", "worker-token-two")]
ASSIGNMENTS = [
    {"run_id": "kitaru-run-1", "job_id": "job-1", "worker_id": "worker-1", "worker_token": "worker-token-one"},
    {"run_id": "kitaru-run-1", "job_id": "job-2", "worker_id": "worker-2", "worker_token": "worker-token-two"},
]
RESULT = {
    "run_id": "kitaru-run-1", "outcome": "green", "recorded_cost_usd": "0.02",
    "cost_incomplete": False, "rows": [{"case": "case-1", "passed": True}],
}
REFUSED = {"detail": "The backend refused the credential of the Loop task."}


def no_kitaru(request):
    raise AssertionError(f"The route called Kitaru: {request.method} {request.url.path}")


def hashed(secret):
    return sha256(secret.encode()).hexdigest()


class LoopTaskPollTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.kitaru = FakeKitaru(no_kitaru)
        self.api = self.enterContext(client(self.kitaru, store=self.store, loop_steps=None))
        self.addCleanup(lambda: self.assertEqual(self.kitaru.requests, []))
        self.loop_id = self.store.start_loop(START, PLAN).loop_id
        self.path = f"{LOOPS}/{self.loop_id}/task/poll"

    def add_task(self, secret=SECRET):
        return self.store.add_loop_task(OWNER, self.loop_id, hashed(secret), experiment=0, run=1)

    def open_run(self, deadline=timedelta(hours=1)):
        self.deadline = datetime.now(timezone.utc) + deadline
        self.assertTrue(self.store.record_run_id(OWNER, self.loop_id, 0, 1, "kitaru-run-1"))
        self.store.seed_assignments(self.loop_id, 0, 1, self.deadline, WORKERS)

    def poll(self, body=POLL, headers=TASK, path=None):
        return self.api.post(path or self.path, headers=headers, json=body)

    def task(self):
        return self.store.latest_loop_task(self.loop_id)

    def assert_refused(self, response):
        self.assertEqual((response.status_code, response.json()), (401, REFUSED))
        self.assertEqual(response.headers["WWW-Authenticate"], "EvalveLoopTask")

    def test_a_poll_returns_the_assignments_of_the_open_run(self):
        self.add_task()
        self.open_run()
        response = self.poll()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(body["assignments"], ASSIGNMENTS)
        self.assertEqual(datetime.fromisoformat(body["run_deadline"]), self.deadline)
        # The poll reads state. A second poll gets the same Assignments.
        self.assertEqual(self.poll().json(), body)
        # `last` is false when the body leaves it out.
        self.assertEqual(self.poll({"contract_version": 1}).json(), body)

    def test_a_poll_returns_no_assignment_when_no_run_is_open(self):
        self.add_task()
        empty = {"run_deadline": None, "assignments": []}
        self.assertEqual(self.poll().json(), empty)
        self.open_run()
        self.assertEqual(len(self.poll().json()["assignments"]), 2)
        self.assertTrue(self.store.collect_run(
            OWNER, self.loop_id, 0, 1, RESULT, datetime.now(timezone.utc), needs_change=False))
        response = self.poll()
        self.assertEqual((response.status_code, response.json()), (200, empty))

    def test_a_poll_returns_no_assignment_when_the_deadline_is_past(self):
        self.add_task()
        self.open_run(deadline=timedelta(seconds=-1))
        response = self.poll()
        self.assertEqual((response.status_code, response.json()), (200, {"run_deadline": None, "assignments": []}))

    def test_the_first_good_poll_makes_the_loop_task_alive(self):
        self.add_task()
        self.assertEqual((self.task()["state"], self.task()["first_poll_at"]), ("starting", None))
        before = datetime.now(timezone.utc)
        self.assertEqual(self.poll().status_code, 200)
        first = self.task()
        self.assertEqual((first["state"], first["contract_version"]), ("alive", 1))
        self.assertEqual(first["first_poll_at"], first["last_poll_at"])
        self.assertLessEqual(before, first["first_poll_at"])
        self.assertEqual(self.poll().status_code, 200)
        second = self.task()
        self.assertEqual((second["state"], second["first_poll_at"]), ("alive", first["first_poll_at"]))
        self.assertGreater(second["last_poll_at"], first["last_poll_at"])

    def test_the_last_poll_ends_the_secret(self):
        self.add_task()
        self.open_run()
        response = self.poll(POLL | {"last": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.task()["state"], "exited")
        self.assert_refused(self.poll())
        self.assert_refused(self.poll(POLL | {"last": True}))

    def test_a_refused_poll_gets_401_and_no_reason(self):
        self.assert_refused(self.poll())  # The Loop has no Loop task.
        self.add_task(OLD_SECRET)
        self.assert_refused(self.poll())  # A wrong secret.
        self.assertEqual(self.poll(headers=OLD_TASK).status_code, 200)
        self.add_task()
        self.assert_refused(self.poll(headers=OLD_TASK))  # The secret of an older row.
        self.assert_refused(self.poll(path=f"{LOOPS}/no-such-loop/task/poll"))
        other = self.store.start_loop(replace(START, slug="other", idempotency_key="start-2"), PLAN).loop_id
        self.assert_refused(self.poll(path=f"{LOOPS}/{other}/task/poll"))  # The secret of a different Loop.
        for headers in ({}, {"Authorization": SECRET}, {"Authorization": "EvalveLoopTask"},
                        {"Authorization": "EvalveLoopTask "}, {"Authorization": f"Basic {SECRET}"}):
            with self.subTest(headers=headers):
                self.assert_refused(self.poll(headers=headers))
        self.assertEqual(self.task()["state"], "starting")
        self.assertEqual(self.poll(headers={"Authorization": f"evalvelooptask {SECRET}"}).status_code, 200)

        self.assertTrue(self.store.end_loop_task(self.loop_id, 2, "stopped"))
        self.assert_refused(self.poll())  # A row that is stopped.

    def test_a_loop_at_an_end_state_refuses_the_poll(self):
        self.add_task()
        self.assertEqual(self.poll().status_code, 200)
        self.assertTrue(self.store.transition(OWNER, self.loop_id, ("running",), "cancelled", "stopped_by_user"))
        self.assert_refused(self.poll())

    def test_a_contract_version_outside_the_range_gets_409_and_a_failed_start(self):
        self.add_task()
        self.open_run()
        response = self.poll(POLL | {"contract_version": 2})
        reason = ("The Supervisor of the agent image uses contract version 2. "
                  "The backend supports versions 1 to 1.")
        self.assertEqual((response.status_code, response.json()), (409, {"detail": reason}))
        task = self.task()
        self.assertEqual((task["state"], task["contract_version"], task["stop_reason"], task["first_poll_at"]),
                         ("failed_start", 2, reason, None))
        self.assert_refused(self.poll())

        self.add_task()
        # A later Supervisor can send a field that this backend does not know. The version is refused, not the body.
        self.assertEqual(self.poll(POLL | {"contract_version": 0, "new_field": True}).status_code, 409)
        self.assertEqual((self.task()["state"], self.task()["contract_version"]), ("failed_start", 0))

    def test_a_refused_secret_wins_over_a_refused_contract_version(self):
        self.add_task()
        self.assert_refused(self.poll(POLL | {"contract_version": 2}, headers=OLD_TASK))
        self.assertEqual(self.task()["state"], "starting")

    def test_a_kitaru_credential_and_a_dashboard_session_get_401(self):
        self.add_task()
        for headers in ({"Authorization": f"Bearer {TOKEN}"}, {"Authorization": f"Bearer {SECRET}"}, SESSION,
                        SESSION | {"Authorization": f"Bearer {TOKEN}"}):
            with self.subTest(headers=headers):
                self.assert_refused(self.poll(headers=headers))
        self.assertEqual(self.task()["state"], "starting")

    def test_the_secret_gets_401_on_each_other_route(self):
        self.add_task()
        detail = f"{LOOPS}/{self.loop_id}"
        requests = [
            ("GET", "/evalve/v1/me", {}), ("GET", LOOPS, {}), ("GET", detail, {}), ("GET", f"{detail}/report", {}),
            ("GET", f"{detail}/experiments/0/runs/1", {}), ("POST", LOOPS, {"json": {}}),
            ("POST", f"{detail}/experiments", {"json": {}}), ("POST", f"{detail}/cancel", {}),
        ]
        paths = {path for path, operations in self.api.app.openapi()["paths"].items() if path != "/evalve/health"}
        # Each route of the API is in the list, except the poll route itself.
        self.assertEqual(len(paths) - 1, len({path for _method, path, _args in requests}))
        for method, path, args in requests:
            with self.subTest(method=method, path=path):
                response = self.api.request(method, path, headers=TASK | {"Idempotency-Key": "test"}, **args)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        # The session cookie is not a second try for the secret.
        self.assertEqual(self.api.get(detail, headers=TASK | SESSION).status_code, 401)

    def test_an_invalid_body_gets_422_only_with_a_good_secret(self):
        self.add_task()
        bodies = ('{"contract_version": ', "{}", '{"contract_version": "1"}', '{"contract_version": 1, "last": "no"}',
                  "[]", '{"contract_version": -1}',
                  '{"contract_version": 1099511627776}')
        for body in bodies:
            with self.subTest(body=body):
                headers = {"Content-Type": "application/json"}
                self.assert_refused(self.api.post(self.path, headers=headers | OLD_TASK, content=body))
                self.assert_refused(self.api.post(self.path, headers=headers, content=body))
                response = self.api.post(self.path, headers=headers | TASK, content=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("Invalid or missing input", response.json()["detail"])
        # An invalid poll is not a poll.
        self.assertEqual((self.task()["state"], self.task()["first_poll_at"]), ("starting", None))

    def test_no_log_line_and_no_error_reply_holds_the_secret_or_a_worker_token(self):
        self.add_task(OLD_SECRET)
        self.add_task()
        self.open_run()
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, logging.WARNING)
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger(__name__).debug("The capture needs one line.")
            good = self.poll()
            replies = [
                self.poll(headers=OLD_TASK),
                self.poll(headers={"Authorization": f"Bearer {SECRET}"}),
                self.api.post(self.path, headers=TASK, content=f'{{"contract_version": "{SECRET}"}}'),
                self.api.post(self.path, headers=TASK, content='{"contract_version": '),
                self.api.get(f"{LOOPS}/{self.loop_id}", headers=TASK),
                self.poll(POLL | {"contract_version": 2}),
                self.poll(),
            ]
        self.assertEqual(good.status_code, 200)
        self.assertEqual([reply.status_code for reply in replies], [401, 401, 422, 422, 401, 409, 401])
        logged = "\n".join(record.getMessage() + str(record.exc_info or "") for record in captured.records)
        for secret in (SECRET, OLD_SECRET, hashed(SECRET), "worker-token-one", "worker-token-two"):
            self.assertNotIn(secret, logged)
            for reply in replies:
                self.assertNotIn(secret, reply.text + str(reply.headers))
        self.assertNotIn(SECRET, good.text)
        # The store holds the hash of the secret, never the secret.
        self.assertNotIn(SECRET, repr(self.store._tasks))
        self.assertEqual(self.task()["secret_hash"], hashed(SECRET))


if __name__ == "__main__":
    unittest.main()
