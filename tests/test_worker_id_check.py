"""The `worker_id` check, with the executor setting `fargate`: only the workers of the Loop ran the
tasks that gave a Run its score.

The tests use the v1 routes, the real Loop steps, a fake Kitaru and a memory store. The fake gives
each task of a finished run the worker of its job, and a test then changes a task.
"""

import unittest
from unittest import mock

from evalve_backend import kitaru_api
from test_run_workers import JOBS, ONE_WORKER, WORKERS, RunWorkersTest
from test_steps import LOOPS, OWNER, SESSION

TASKS = "/api/v1/tasks"
FOREIGN_WORKER = "worker-of-a-teammate"


def reason(count: int) -> str:
    return (f"A worker that does not belong to this Loop ran {count} tasks of this Run. While a Loop runs, "
            "stop each worker that has an `agent` claim, an `evaluator` claim or no claim.")


class WorkerIdCheckTest(RunWorkersTest):
    def tasks(self, kind: str) -> list[dict]:
        """The tasks of this kind of the newest run."""
        jobs = {job_id for job_id, job in self.kitaru.jobs.items() if job["experiment_run_id"] == self.latest_run()}
        return [task for task in self.kitaru.tasks.values() if task["job_id"] in jobs and task["kind"] == kind]

    def task_calls(self) -> list:
        return self.kitaru.requests_to("GET", TASKS)

    def checked(self, loop_id, experiment=0, run=1) -> tuple:
        """What the check left in the result of a Run, as the status reply shows it."""
        result = self.status(loop_id, SESSION)["experiments"][experiment]["runs"][run - 1]["result"]
        return result["outcome"], result["run_error"], result["foreign_tasks"]


class ForeignTaskTest(WorkerIdCheckTest):
    def test_a_green_run_with_a_completed_evaluator_task_of_a_different_worker_is_incomplete(self):
        loop_id = self.started()
        self.finish("green")
        self.tasks("evaluator")[0]["worker_id"] = FOREIGN_WORKER
        self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))

    def test_a_green_run_with_a_completed_agent_task_of_a_different_worker_is_incomplete(self):
        loop_id = self.started()
        self.finish("green")
        self.tasks("agent")[1]["worker_id"] = FOREIGN_WORKER
        self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))

    def test_a_red_run_with_a_task_of_a_different_worker_is_incomplete(self):
        loop_id = self.started()
        self.finish("red")
        self.tasks("agent")[0]["worker_id"] = FOREIGN_WORKER
        self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))

    def test_a_completed_task_with_an_empty_worker_id_fails_the_check(self):
        loop_id = self.started()
        self.finish("green")
        self.tasks("evaluator")[0]["worker_id"] = None
        self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))

    def test_a_job_with_no_row_and_a_completed_task_fails_the_check(self):
        self.fail_registration(2)
        self.assertEqual(self.start().status_code, 503)
        loop_id = self.store.list_loops(None)[0]["loop_id"]
        self.assertEqual(len(self.rows(loop_id)), 1)
        self.finish("green")
        without_row = sorted(set(self.kitaru.jobs) - {row["job_id"] for row in self.rows(loop_id)})
        tasks = [task for task in self.kitaru.tasks.values() if task["job_id"] in without_row]
        self.assertEqual([task["kind"] for task in tasks], ["agent", "evaluator"])
        # A worker that the backend does not know ran the agent task. Nothing ran the evaluator task yet.
        tasks[0]["worker_id"] = FOREIGN_WORKER
        tasks[1]["status"] = "pending"
        self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))


class PassedCheckTest(WorkerIdCheckTest):
    def test_a_green_run_whose_completed_tasks_each_have_the_worker_of_their_job_stays_green(self):
        loop_id = self.started()
        self.finish("green")
        by_job = {worker["scope"]["job_id"]: worker_id for worker_id, worker in self.kitaru.workers.items()}
        ran = [(task["kind"], task["status"], task["worker_id"] == by_job[task["job_id"]]) for task in self.kitaru.tasks.values()]
        self.assertEqual(sorted(ran), [("agent", "completed", True)] * 2 + [("evaluator", "completed", True)] * 2)
        self.assertEqual(self.checked(loop_id), ("green", None, None))
        # One task list for each job of the Run.
        self.assertEqual(len(self.task_calls()), 2)

    def test_a_task_of_a_different_worker_that_is_failed_or_canceled_does_not_fail_the_check(self):
        loop_id = self.started()
        self.finish("green")
        job_id = sorted(self.kitaru.jobs)[0]
        self.kitaru.add_task(job_id, "agent", "failed", FOREIGN_WORKER)
        self.kitaru.add_task(job_id, "evaluator", "canceled", FOREIGN_WORKER)
        self.assertEqual(self.checked(loop_id), ("green", None, None))

    def test_a_completed_task_of_a_different_kind_is_not_read(self):
        loop_id = self.started()
        self.finish("green")
        self.kitaru.add_task(sorted(self.kitaru.jobs)[0], "analyzer", "completed", FOREIGN_WORKER)
        self.assertEqual(self.checked(loop_id), ("green", None, None))

    def test_a_run_that_the_score_made_incomplete_gets_no_task_list_call(self):
        loop_id = self.started()
        self.finish("incomplete")
        self.tasks("evaluator")[0]["worker_id"] = FOREIGN_WORKER
        mark = len(self.kitaru.requests_to("GET", JOBS))
        self.assertEqual(self.checked(loop_id), ("incomplete", None, None))
        self.assertEqual(self.task_calls(), [])
        self.assertEqual(len(self.kitaru.requests_to("GET", JOBS)), mark)

    def test_a_job_with_more_tasks_than_one_page_is_read_to_its_end(self):
        loop_id = self.started()
        self.finish("green")
        job_id = sorted(self.kitaru.jobs)[0]
        worker_id = self.tasks("agent")[0]["worker_id"]
        for _ in range(4):
            self.kitaru.add_task(job_id, "evaluator", "completed", worker_id)
        # The last task of the last page.
        self.kitaru.add_task(job_id, "evaluator", "completed", FOREIGN_WORKER)
        with mock.patch.object(kitaru_api, "PAGE_SIZE", 2):
            self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))
        pages = [request.url.params.get("cursor") for request in self.task_calls() if job_id in request.url.params["filter"]]
        self.assertEqual(pages, [None, "2", "4", "6"])


class RulesAfterTheCheckTest(WorkerIdCheckTest):
    def test_a_run_that_fails_the_check_gets_a_retry(self):
        loop_id = self.started()
        self.finish("green")
        self.tasks("agent")[0]["worker_id"] = FOREIGN_WORKER
        self.status(loop_id)
        self.assertEqual(self.outcomes(loop_id), [["incomplete", None]])
        self.assertEqual({item["run_id"] for item in self.poll(loop_id).assignments}, {self.latest_run()})

    def test_two_failed_checks_in_a_row_end_the_loop(self):
        loop_id = self.started()
        for _ in range(2):
            self.finish("green")
            self.tasks("evaluator")[0]["worker_id"] = FOREIGN_WORKER
            self.status(loop_id)
        self.assertEqual(self.outcomes(loop_id), [["incomplete", "incomplete"]])
        self.assertEqual(self.end(loop_id), ("failed", "error"))
        self.assertEqual(self.kitaru.workers, {})


class OrderTest(WorkerIdCheckTest):
    def paths(self) -> list[str]:
        return [request.url.path for request in self.kitaru.requests]

    def assert_check_before_delete(self) -> None:
        paths = self.paths()
        last_check = max(index for index, path in enumerate(paths) if path == TASKS)
        first_delete = min(index for index, path in enumerate(paths) if path.startswith(WORKERS + "/"))
        self.assertLess(last_check, first_delete)

    def test_a_refresh_does_the_check_before_the_delete_of_the_workers(self):
        loop_id = self.started()
        self.finish("green")
        self.status(loop_id, SESSION)
        self.assert_check_before_delete()
        self.assertEqual(self.outcomes(loop_id), [["green"]])
        self.assertEqual(self.kitaru.workers, {})

    def test_a_drive_does_the_check_before_the_delete_of_the_workers(self):
        loop_id = self.started()
        self.finish("green")
        self.tasks("agent")[0]["worker_id"] = FOREIGN_WORKER
        self.status(loop_id)
        self.assert_check_before_delete()
        self.assertEqual(self.outcomes(loop_id), [["incomplete", None]])


class KitaruCannotAnswerTest(WorkerIdCheckTest):
    def assert_503_then_collect(self, loop_id, headers) -> None:
        reply = self.api.get(f"{LOOPS}/{loop_id}", headers=headers)
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertIsNone(self.run_result(loop_id))
        self.assertEqual(self.kitaru.requests_to("DELETE", ONE_WORKER), [])
        self.assertEqual(len(self.kitaru.workers), 2)
        self.assertEqual([row["has_token"] for row in self.rows(loop_id)], [True, True])
        # The next call collects the Run.
        self.assertEqual(self.checked(loop_id), ("incomplete", reason(1), 1))
        self.assertEqual(self.kitaru.workers, {})

    def fail_the_second_task_list(self) -> str:
        """A finished Run with one foreign task, whose first job Kitaru can list and whose second it cannot."""
        loop_id = self.started()
        self.finish("green")
        self.tasks("evaluator")[1]["worker_id"] = FOREIGN_WORKER
        seen = []

        def count():
            seen.append(1)
            if len(seen) == 2:
                self.kitaru.fail("GET", TASKS, 503)

        self.kitaru.observe("GET", TASKS, count)
        return loop_id

    def test_a_refresh_with_a_task_list_that_kitaru_cannot_answer_gives_503_and_no_result(self):
        self.assert_503_then_collect(self.fail_the_second_task_list(), SESSION)

    def test_a_drive_with_a_task_list_that_kitaru_cannot_answer_gives_503_and_no_result(self):
        self.assert_503_then_collect(self.fail_the_second_task_list(), OWNER)

    def test_a_job_list_that_kitaru_cannot_answer_gives_503_and_no_result(self):
        loop_id = self.started()
        self.finish("green")
        self.tasks("evaluator")[1]["worker_id"] = FOREIGN_WORKER
        self.kitaru.fail("GET", JOBS, 503)
        self.assert_503_then_collect(loop_id, SESSION)

    def test_a_run_that_is_gone_at_the_check_is_incomplete(self):
        loop_id = self.started()
        self.finish("green")
        self.kitaru.fail("GET", JOBS, 404, "Experiment run not found.")
        self.assertEqual(self.checked(loop_id), ("incomplete", "The Kitaru run no longer exists.", None))
        self.assertEqual(self.task_calls(), [])

    def test_a_task_list_that_kitaru_refuses_ends_the_loop_and_stores_no_result(self):
        # The check never passes a Run that it could not read. A refusal comes again with each call,
        # so the Loop ends with the reason, as for each other request that Kitaru refuses.
        loop_id = self.started()
        self.finish("green")
        self.kitaru.fail("GET", TASKS, 422, "Unknown filter field.")
        detail = self.status(loop_id, SESSION)
        self.assertEqual((detail["status"], detail["outcome"]), ("failed", "error"))
        self.assertIn("GET /tasks, HTTP 422: Unknown filter field.", detail["reason"])
        self.assertIsNone(self.run_result(loop_id))
        self.assertEqual(self.kitaru.workers, {})


if __name__ == "__main__":
    unittest.main()
