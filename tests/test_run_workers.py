"""The workers of a Run, with the executor setting `fargate`: register, give out and delete.

The tests use the v1 routes, the real Loop steps, a fake Kitaru and a memory store. They read the
Assignments as a Loop task does, through a poll.
"""

import json
import logging
import unittest
from datetime import datetime, timedelta, timezone

from fake_kitaru import WORKER_TOKEN_LIFETIME
from test_steps import LOOPS, OWNER, SESSION, StepsTest

ACCOUNT = "account-1"
SECRET_HASH = "a" * 64
WORKERS = "/api/v1/workers"
ONE_WORKER = r"/api/v1/workers/[^/]+"
JOBS = r"/api/v1/experiment-runs/[^/]+/jobs"
CANCEL = r"/api/v1/experiment-runs/[^/]+/cancel"


class RunWorkersTest(StepsTest):
    EXECUTOR = "fargate"

    def rows(self, loop_id, experiment=0, run=1) -> list[dict]:
        return self.store.run_workers(ACCOUNT, loop_id, experiment, run)

    def poll(self, loop_id):
        """What the Loop task of the Loop reads now."""
        if self.store.latest_loop_task(loop_id) is None:
            self.store.add_loop_task(ACCOUNT, loop_id, SECRET_HASH)
        return self.store.record_poll(loop_id, SECRET_HASH, 1, datetime.now(timezone.utc))

    def registrations(self) -> list[dict]:
        return [json.loads(request.content) for request in self.kitaru.requests_to("POST", WORKERS)]

    def fail_registration(self, number: int) -> None:
        """Make Kitaru refuse the registration with this number with a transient error."""
        seen = []

        def count():
            seen.append(1)
            if len(seen) == number:
                self.kitaru.fail("POST", WORKERS, 500)

        self.kitaru.observe("POST", WORKERS, count)

    def two_hours_later(self, loop_id) -> None:
        """Move each time of the open Run two hours back, so its deadline is past."""
        run = self.store._loops[loop_id]["experiments"][-1]["runs"][-1]
        for time in ("submit_started_at", "start_sent_at", "deadline_at"):
            run[time] -= timedelta(hours=2)

    def run_result(self, loop_id, experiment=0, run=1) -> dict:
        return self.stored(loop_id)["experiments"][experiment]["runs"][run - 1]["result"]


class RegistrationTest(RunWorkersTest):
    def test_a_drive_that_starts_a_run_registers_one_worker_for_each_job(self):
        loop_id = self.started()
        run_id = self.latest_run()
        jobs = sorted(self.kitaru.jobs)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sorted((body["scope"] for body in self.registrations()), key=lambda scope: scope["job_id"]), [
            {"claims": [{"kind": "agent"}, {"kind": "evaluator"}], "job_id": job_id} for job_id in jobs
        ])
        for request in self.kitaru.requests_to("POST", WORKERS):
            self.assertEqual(request.headers["authorization"], OWNER["Authorization"])
        by_job = {worker["scope"]["job_id"]: worker for worker in self.kitaru.workers.values()}
        poll = self.poll(loop_id)
        self.assertEqual(list(poll.assignments), [
            {"run_id": run_id, "job_id": job_id, "worker_id": by_job[job_id]["id"],
             "worker_token": self.kitaru.worker_tokens[by_job[job_id]["id"]]}
            for job_id in jobs
        ])
        rows = self.rows(loop_id)
        self.assertEqual([(row["job_id"], row["worker_id"], row["has_token"], row["worker_deleted_at"]) for row in rows],
                         [(job_id, by_job[job_id]["id"], True, None) for job_id in jobs])
        # Kitaru gives the expiry of each token. The deadline of the Run is the expiry of the first worker.
        self.assertEqual(poll.run_deadline, min(row["token_expires_at"] for row in rows))
        self.assertLess(abs(poll.run_deadline - (datetime.now(timezone.utc) + WORKER_TOKEN_LIFETIME)), timedelta(minutes=1))

    def test_a_drive_of_a_run_that_has_each_worker_registers_no_more(self):
        loop_id = self.started()
        mark = len(self.kitaru.requests)
        self.assertEqual(self.status(loop_id)["next"]["action"], "collect")
        paths = [request.url.path for request in self.kitaru.requests[mark:]]
        self.assertEqual([path for path in paths if path.endswith("/jobs") or path == WORKERS], [])

    def test_the_next_drive_registers_only_the_workers_that_are_missing(self):
        for member in ("recorded-3", "recorded-4", "recorded-5"):
            self.kitaru.add_member(member)
        self.fail_registration(3)
        self.assertEqual(self.start().status_code, 503)
        loop_id = self.store.list_loops(None)[0]["loop_id"]
        first = self.rows(loop_id)
        self.assertEqual(len(first), 2)
        deadline = self.poll(loop_id).run_deadline
        self.assertEqual(deadline, first[0]["token_expires_at"])

        self.status(loop_id)
        rows = self.rows(loop_id)
        self.assertEqual(sorted(row["job_id"] for row in rows), sorted(self.kitaru.jobs))
        self.assertEqual(len(self.kitaru.workers), 5)
        # Two before the failure, the one that failed, and three after it.
        self.assertEqual(len(self.registrations()), 6)
        self.assertEqual(len(self.poll(loop_id).assignments), 5)
        # A later worker has a later expiry, and the deadline stays.
        self.assertGreater(max(row["token_expires_at"] for row in rows), deadline)
        self.assertEqual(self.poll(loop_id).run_deadline, deadline)

        self.status(loop_id)
        self.assertEqual(len(self.registrations()), 6)

    def test_a_refresh_of_a_run_with_missing_workers_registers_none(self):
        self.fail_registration(2)
        self.assertEqual(self.start().status_code, 503)
        loop_id = self.store.list_loops(None)[0]["loop_id"]
        self.assertEqual(self.status(loop_id, SESSION)["next"]["action"], "collect")
        self.assertEqual(len(self.rows(loop_id)), 1)
        self.assertEqual(len(self.registrations()), 2)
        self.assertEqual(self.kitaru.requests_to("GET", JOBS)[1:], [])

    def test_the_first_run_of_a_new_experiment_gets_its_own_workers(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        self.assertEqual(self.propose(loop_id).status_code, 201)
        self.assertEqual(len(self.rows(loop_id, 1, 1)), 2)
        self.assertEqual({item["run_id"] for item in self.poll(loop_id).assignments}, {self.latest_run()})


class DeadlineTest(RunWorkersTest):
    REASON = "The Run passed its deadline of 60 minutes."

    def test_after_the_deadline_the_poll_gives_no_assignment(self):
        loop_id = self.started()
        self.assertEqual(len(self.poll(loop_id).assignments), 2)
        mark = len(self.kitaru.requests)
        self.two_hours_later(loop_id)
        poll = self.poll(loop_id)
        self.assertEqual((poll.run_deadline, poll.assignments), (None, ()))
        self.assertEqual(self.kitaru.requests[mark:], [])

    def test_after_the_deadline_a_drive_cancels_the_open_run_and_records_it_as_incomplete(self):
        loop_id = self.started()
        first_run = self.latest_run()
        self.two_hours_later(loop_id)
        self.status(loop_id)
        self.assertEqual(self.kitaru.runs[first_run]["status"], "canceled")
        result = self.run_result(loop_id)
        self.assertEqual((result["outcome"], result["run_error"]), ("incomplete", self.REASON))
        # The existing rules for an incomplete Run apply: the drive starts the retry.
        self.assertEqual(self.outcomes(loop_id), [["incomplete", None]])
        self.assertEqual({item["run_id"] for item in self.poll(loop_id).assignments}, {self.latest_run()})

    def test_after_the_deadline_a_refresh_cancels_the_open_run_and_records_it_as_incomplete(self):
        loop_id = self.started()
        self.two_hours_later(loop_id)
        self.status(loop_id, SESSION)
        self.assertEqual(self.kitaru.runs[self.latest_run()]["status"], "canceled")
        result = self.run_result(loop_id)
        self.assertEqual((result["outcome"], result["run_error"]), ("incomplete", self.REASON))
        self.assertEqual(self.outcomes(loop_id), [["incomplete"]])
        self.assertEqual(self.poll(loop_id).assignments, ())

    def test_a_run_that_finished_before_its_deadline_is_scored_after_it(self):
        loop_id = self.started()
        self.finish("green")
        self.two_hours_later(loop_id)
        self.status(loop_id, SESSION)
        self.assertEqual(self.outcomes(loop_id), [["green"]])
        self.assertEqual(self.kitaru.requests_to("POST", CANCEL), [])

    def test_a_run_that_finishes_while_the_backend_cancels_it_is_scored(self):
        loop_id = self.started()
        self.two_hours_later(loop_id)
        self.kitaru.observe("POST", CANCEL, lambda: self.finish("green"))
        self.status(loop_id, SESSION)
        self.assertEqual(self.outcomes(loop_id), [["green"]])


class CollectTest(RunWorkersTest):
    def test_a_refresh_stores_the_result_then_erases_the_tokens_then_deletes_the_workers(self):
        loop_id = self.started()
        self.finish("green")
        seen = []

        def at_the_delete():
            stored = self.store.get_run(None, loop_id, 0, 1)
            seen.append((stored["result"]["outcome"], [row["has_token"] for row in self.rows(loop_id)]))

        self.kitaru.observe("DELETE", ONE_WORKER, at_the_delete)
        self.assertEqual(len(self.kitaru.workers), 2)
        self.status(loop_id, SESSION)
        self.assertEqual(seen, [("green", [False, False])] * 2)
        self.assertEqual(self.kitaru.workers, {})
        self.assertTrue(all(row["worker_deleted_at"] is not None for row in self.rows(loop_id)))
        # The delete is the last Kitaru call about the Run: Kitaru then forgets which worker ran a task.
        paths = [request.url.path for request in self.kitaru.requests]
        last_evidence = max(index for index, path in enumerate(paths) if path in ("/api/v1/replays", "/api/v1/evaluations", "/api/v1/sessions"))
        first_delete = min(index for index, path in enumerate(paths) if path.startswith(WORKERS + "/"))
        self.assertLess(last_evidence, first_delete)

    def test_a_delete_that_fails_does_not_fail_the_collect_and_the_next_call_deletes_the_worker(self):
        loop_id = self.started(required_consecutive_greens=3)
        self.finish("green")
        self.kitaru.fail("DELETE", ONE_WORKER, 503)
        detail = self.status(loop_id, SESSION)
        self.assertEqual(detail["experiments"][0]["runs"][0]["result"]["outcome"], "green")
        self.assertEqual(len(self.kitaru.workers), 2)
        self.assertEqual([row["worker_deleted_at"] for row in self.rows(loop_id)], [None, None])
        self.assertEqual([row["has_token"] for row in self.rows(loop_id)], [False, False])
        self.status(loop_id, SESSION)
        self.assertEqual(self.kitaru.workers, {})
        self.assertTrue(all(row["worker_deleted_at"] is not None for row in self.rows(loop_id)))
        # A worker is deleted one time.
        self.status(loop_id, SESSION)
        self.assertEqual(len(self.kitaru.requests_to("DELETE", ONE_WORKER)), 3)

    def test_a_call_after_the_loop_ended_deletes_a_worker_that_is_left(self):
        loop_id = self.started(required_consecutive_greens=1)
        self.finish("green")
        # Kitaru cannot delete at the collect, and not at the end of the Loop.
        self.kitaru.fail("DELETE", ONE_WORKER, 503)
        self.kitaru.fail("DELETE", ONE_WORKER, 503)
        self.assertEqual(self.status(loop_id, SESSION)["status"], "completed")
        self.assertEqual(len(self.kitaru.workers), 2)
        self.assertEqual(self.status(loop_id, SESSION)["status"], "completed")
        self.assertEqual(self.kitaru.workers, {})
        self.assertTrue(all(row["worker_deleted_at"] is not None for row in self.rows(loop_id)))

    def test_a_call_to_a_loop_that_waits_for_a_change_deletes_a_worker_that_is_left(self):
        loop_id = self.started()
        self.finish("red")
        self.kitaru.fail("DELETE", ONE_WORKER, 503)
        self.assertEqual(self.status(loop_id)["next"]["action"], "needs_change")
        self.assertEqual(len(self.kitaru.workers), 2)
        self.assertEqual(self.status(loop_id)["next"]["action"], "needs_change")
        self.assertEqual(self.kitaru.workers, {})

    def test_a_loop_that_ends_because_kitaru_refuses_a_worker_cancels_its_open_run(self):
        self.kitaru.fail("POST", WORKERS, 422, "Invalid worker.")
        reply = self.start()
        # The Run has its Kitaru ID, so the start call answers with the Loop, which is failed.
        self.assertEqual((reply.status_code, reply.json()["status"], reply.json()["outcome"]), (201, "failed", "error"))
        self.assertIn("POST /workers, HTTP 422", reply.json()["reason"])
        self.assertEqual(self.kitaru.runs[self.latest_run()]["status"], "canceled")

    def test_a_worker_that_kitaru_refuses_to_delete_does_not_stop_the_others(self):
        loop_id = self.started()
        self.finish("green")
        self.kitaru.fail("DELETE", ONE_WORKER, 400)
        self.status(loop_id, SESSION)
        self.assertEqual(len(self.kitaru.workers), 1)
        self.assertEqual(sum(row["worker_deleted_at"] is None for row in self.rows(loop_id)), 1)

    def test_a_worker_that_is_gone_from_kitaru_counts_as_deleted(self):
        loop_id = self.started()
        self.finish("green")
        self.kitaru.workers.clear()
        self.status(loop_id, SESSION)
        self.assertEqual(len(self.kitaru.requests_to("DELETE", ONE_WORKER)), 2)
        self.assertTrue(all(row["worker_deleted_at"] is not None for row in self.rows(loop_id)))

    def test_the_call_that_ends_a_loop_deletes_the_workers_of_its_open_run(self):
        loop_id = self.started()
        reply = self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=SESSION)
        self.assertEqual((reply.status_code, reply.json()["status"]), (200, "cancelled"))
        self.assertEqual(self.kitaru.workers, {})
        self.assertEqual([(row["has_token"], row["worker_deleted_at"] is None) for row in self.rows(loop_id)],
                         [(False, False)] * 2)


class WorkerTokenTest(RunWorkersTest):
    def test_no_log_line_holds_a_worker_token(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("evalve_backend").debug("Capture every evalve_backend log line.")
            self.fail_registration(2)
            self.assertEqual(self.start().status_code, 503)
            loop_id = self.store.list_loops(None)[0]["loop_id"]
            self.status(loop_id)
            self.poll(loop_id)
            self.finish("red")
            self.kitaru.fail("DELETE", ONE_WORKER, 503)
            self.status(loop_id)
            self.status(loop_id)
            self.propose(loop_id)
            self.kitaru.fail("POST", WORKERS, 422, "Invalid worker.")
            self.two_hours_later(loop_id)
            self.status(loop_id)
            self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER)
        text = "\n".join(logs.output)
        self.assertGreaterEqual(len(self.kitaru.worker_tokens), 4)
        for token in self.kitaru.worker_tokens.values():
            self.assertNotIn(token, text)
        self.assertNotIn("worker-token-value", repr(self.poll(loop_id)))


class ExecutorSettingKitaruTest(StepsTest):
    def test_with_the_kitaru_executor_no_worker_is_registered_and_no_run_has_a_deadline(self):
        loop_id = self.started(required_consecutive_greens=3)
        self.store.add_loop_task(ACCOUNT, loop_id, SECRET_HASH)
        poll = self.store.record_poll(loop_id, SECRET_HASH, 1, datetime.now(timezone.utc))
        self.assertEqual((poll.run_deadline, poll.assignments), (None, ()))
        self.finish("green")
        self.status(loop_id)
        self.assertEqual(self.outcomes(loop_id), [["green", None]])
        self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER)
        for method, path in (("GET", JOBS), ("POST", WORKERS), ("DELETE", ONE_WORKER)):
            self.assertEqual(self.kitaru.requests_to(method, path), [], path)
        self.assertEqual(self.store.run_workers(ACCOUNT, loop_id, 0, 1), [])


if __name__ == "__main__":
    unittest.main()
