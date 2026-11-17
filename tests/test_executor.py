"""The Kitaru executor against the fake Kitaru: finding its own work again after a restart (sc-315)."""

import unittest
from datetime import timedelta

import httpx

from evalve_backend.executor import ExecutionFailed, ExperimentSpec, KitaruExecutor, RunGone, RunSpec
from evalve_backend.kitaru_api import KitaruApi, KitaruTransient
from fake_kitaru import AGENT_ID, AGENT_VERSION_ID, COHORT_VERSION_ID, EVALUATOR_VERSION_ID, FakeKitaru
from helpers import KITARU_URL, TOKEN

AUTHORIZATION = f"Bearer {TOKEN}"
EXPERIMENTS = r"/api/v1/experiments"


def experiment_spec(**changes) -> ExperimentSpec:
    values = {
        "name": "evalve-loop-1-exp-0", "idempotency_key": "evalve-loop-1-exp-0", "agent_id": AGENT_ID,
        "description": "Round 0: Unchanged control", "override": {}, "tool_policy": {"default": {"type": "replay"}, "tools": {}},
        "evaluators": [{"evaluator": "policy", "version": 1, "version_id": EVALUATOR_VERSION_ID,
                        "params": {"strict": True}, "connection_id": None}],
        "experiment_id": None,
    }
    return ExperimentSpec(**(values | changes))


class ExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kitaru = FakeKitaru(AUTHORIZATION)
        client = httpx.AsyncClient(base_url=KITARU_URL, transport=self.kitaru.transport())
        self.addAsyncCleanup(client.aclose)
        self.executor = KitaruExecutor(KitaruApi(client, AUTHORIZATION))

    def run_spec(self, k: int = 1, **changes) -> RunSpec:
        values = {"idempotency_key": f"evalve-loop-1-exp-0-run-{k}", "cohort_version_id": COHORT_VERSION_ID,
                  "agent_version_id": AGENT_VERSION_ID, "submit_started_at": self.kitaru.now}
        return RunSpec(**(values | changes))

    async def experiment(self) -> str:
        return await self.executor.ensure_experiment(experiment_spec())


class EnsureExperimentTest(ExecutorTest):
    async def test_a_new_experiment_is_created_with_its_name_and_key(self):
        experiment_id = await self.experiment()
        created = self.kitaru.experiments[experiment_id]
        self.assertEqual((created["name"], created["agent_id"]), ("evalve-loop-1-exp-0", AGENT_ID))
        (request,) = self.kitaru.requests_to("POST", EXPERIMENTS)
        self.assertEqual(request.headers["idempotency-key"], "evalve-loop-1-exp-0")
        self.assertNotIn(b'"override"', request.content)

    async def test_a_candidate_experiment_carries_its_override(self):
        experiment_id = await self.executor.ensure_experiment(experiment_spec(override={"model": "gpt-large"}))
        self.assertEqual(self.kitaru.experiments[experiment_id]["override"]["model"], "gpt-large")
        self.assertEqual(await self.executor.ensure_experiment(
            experiment_spec(override={"model": "gpt-large"}, experiment_id=experiment_id)), experiment_id)

    async def test_a_recorded_experiment_is_read_by_id(self):
        experiment_id = await self.experiment()
        self.kitaru.requests.clear()
        self.assertEqual(await self.executor.ensure_experiment(experiment_spec(experiment_id=experiment_id)), experiment_id)
        self.assertEqual([r.url.path for r in self.kitaru.requests], [f"{EXPERIMENTS}/{experiment_id}"])

    async def test_after_a_restart_the_experiment_is_found_by_agent_and_name(self):
        experiment_id = await self.experiment()
        self.kitaru.expire_keys()
        self.kitaru.requests.clear()
        self.assertEqual(await self.experiment(), experiment_id)
        self.assertEqual(self.kitaru.requests_to("POST", EXPERIMENTS), [])
        self.assertEqual(len(self.kitaru.experiments), 1)

    async def test_a_409_on_create_reads_the_experiment_back(self):
        experiment_id = await self.experiment()
        # The first lookup misses, as when the create commits between the lookup and the retry.
        self.kitaru.answer_once("GET", EXPERIMENTS, httpx.Response(200, json={"items": [], "next_cursor": None}))
        self.kitaru.expire_keys()
        self.kitaru.requests.clear()
        self.assertEqual(await self.experiment(), experiment_id)
        # Lookup (missed), create (409, name taken), lookup again (found).
        self.assertEqual([(r.method, r.url.path) for r in self.kitaru.requests],
                         [("GET", EXPERIMENTS), ("POST", EXPERIMENTS), ("GET", EXPERIMENTS)])

    async def test_a_409_whose_experiment_cannot_be_read_back_is_transient(self):
        self.kitaru.fail("POST", EXPERIMENTS, 409, "Experiment name 'evalve-loop-1-exp-0' is already registered")
        with self.assertRaises(KitaruTransient):
            await self.experiment()

    async def test_a_key_still_in_progress_is_transient_with_no_read_back(self):
        self.kitaru.fail("POST", EXPERIMENTS, 409, "Idempotency-Key request is still in progress.")
        with self.assertRaises(KitaruTransient):
            await self.experiment()
        self.assertEqual(len(self.kitaru.requests_to("GET", EXPERIMENTS)), 1)

    async def test_a_lost_create_reply_is_retried_with_the_same_bytes(self):
        self.kitaru.lose_next_reply("POST", EXPERIMENTS)
        self.kitaru.answer_once("GET", EXPERIMENTS, httpx.Response(200, json={"items": [], "next_cursor": None}))
        with self.assertRaises(KitaruTransient):
            await self.experiment()
        # Inside the key window the lookup finds it anyway. Force the create path to show the replay.
        self.kitaru.answer_once("GET", EXPERIMENTS, httpx.Response(200, json={"items": [], "next_cursor": None}))
        experiment_id = await self.experiment()
        first, second = self.kitaru.requests_to("POST", EXPERIMENTS)
        self.assertEqual(first.content, second.content)
        self.assertEqual(len(self.kitaru.experiments), 1)
        self.assertIn(experiment_id, self.kitaru.experiments)

    async def test_an_experiment_that_someone_changed_fails_the_loop(self):
        changes = {
            "override": lambda e: e["override"].update(system_prompt="Be terse."),
            "tool_policy": lambda e: e.update(tool_policy={"default": {"type": "passthrough"}}),
            "evaluators": lambda e: e["evaluators"][0].update(params={"strict": False}),
            "agent": lambda e: e.update(agent_id="agent-2"),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.kitaru.experiments.clear()
                self.kitaru.expire_keys()
                experiment_id = await self.experiment()
                change(self.kitaru.experiments[experiment_id])
                with self.assertRaises(ExecutionFailed) as caught:
                    await self.executor.ensure_experiment(experiment_spec(experiment_id=experiment_id))
                self.assertIn("no longer matches", str(caught.exception))

    async def test_a_recorded_experiment_that_is_gone_fails_the_loop(self):
        with self.assertRaises(ExecutionFailed) as caught:
            await self.executor.ensure_experiment(experiment_spec(experiment_id="experiment-404"))
        self.assertIn("no longer exists", str(caught.exception))


class SubmitTest(ExecutorTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.experiment_id = await self.experiment()

    async def test_a_run_is_started_with_its_key(self):
        run_id = await self.executor.submit(self.experiment_id, self.run_spec(), set())
        self.assertEqual(self.kitaru.runs[run_id]["agent_version_id"], AGENT_VERSION_ID)
        (request,) = self.kitaru.requests_to("POST", rf"{EXPERIMENTS}/[^/]+/runs")
        self.assertEqual(request.headers["idempotency-key"], "evalve-loop-1-exp-0-run-1")
        self.assertEqual(request.content, (
            b'{"agent_version_id":"agent-version-2","baseline_evaluation_mode":"none",'
            b'"cohort_version_id":"6f1c2c4e-8f4b-4a57-9a51-3c0e2a1d5b10","evaluate_baselines":false}'))

    async def test_a_lost_submit_reply_is_adopted_by_the_next_call(self):
        spec = self.run_spec()
        self.kitaru.lose_next_reply("POST", f"{EXPERIMENTS}/{self.experiment_id}/runs")
        with self.assertRaises(KitaruTransient):
            await self.executor.submit(self.experiment_id, spec, set())
        run_id = await self.executor.submit(self.experiment_id, spec, set())
        self.assertEqual(list(self.kitaru.runs), [run_id])
        self.assertEqual(len(self.kitaru.requests_to("POST", rf"{EXPERIMENTS}/[^/]+/runs")), 1)

    async def test_a_retry_inside_the_key_window_sends_the_same_bytes_and_gets_the_same_run(self):
        spec = self.run_spec()
        first = await self.executor.submit(self.experiment_id, spec, set())
        # The list shows the run as known, so the executor submits again. The key replays the answer.
        self.assertEqual(await self.executor.submit(self.experiment_id, spec, {first}), first)
        one, two = self.kitaru.requests_to("POST", rf"{EXPERIMENTS}/[^/]+/runs")
        self.assertEqual(one.content, two.content)
        self.assertEqual(len(self.kitaru.runs), 1)

    async def test_after_the_key_expired_the_run_is_adopted_and_no_second_run_starts(self):
        spec = self.run_spec()
        self.kitaru.lose_next_reply("POST", f"{EXPERIMENTS}/{self.experiment_id}/runs")
        with self.assertRaises(KitaruTransient):
            await self.executor.submit(self.experiment_id, spec, set())
        self.kitaru.tick(16 * 60)
        self.kitaru.expire_keys()
        run_id = await self.executor.submit(self.experiment_id, spec, set())
        self.assertEqual(list(self.kitaru.runs), [run_id])

    async def test_the_oldest_of_two_candidates_is_adopted(self):
        spec = self.run_spec()
        older = self.kitaru.add_run(self.experiment_id)["id"]
        self.kitaru.add_run(self.experiment_id)
        self.assertEqual(await self.executor.submit(self.experiment_id, spec, set()), older)
        self.assertEqual(self.kitaru.requests_to("POST", rf"{EXPERIMENTS}/[^/]+/runs"), [])

    async def test_runs_that_do_not_qualify_are_ignored_and_a_new_run_starts(self):
        spec = self.run_spec()
        known = self.kitaru.add_run(self.experiment_id)["id"]
        self.kitaru.add_run(self.experiment_id, agent_version_id="agent-version-1")
        self.kitaru.add_run(self.experiment_id, cohort_version_id="other-cohort-version")
        self.kitaru.add_run(self.experiment_id, created=spec.submit_started_at - timedelta(seconds=61))
        run_id = await self.executor.submit(self.experiment_id, spec, {known})
        self.assertEqual(len(self.kitaru.runs), 5)
        self.assertEqual(self.kitaru.runs[run_id]["number"], 5)

    async def test_a_run_up_to_60_seconds_before_the_submit_started_is_adopted(self):
        spec = self.run_spec()
        early = self.kitaru.add_run(self.experiment_id, created=spec.submit_started_at - timedelta(seconds=60))["id"]
        self.assertEqual(await self.executor.submit(self.experiment_id, spec, set()), early)


class RunTest(ExecutorTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.experiment_id = await self.experiment()
        self.run_id = await self.executor.submit(self.experiment_id, self.run_spec(), set())

    async def test_poll_reads_the_run(self):
        self.assertEqual((await self.executor.poll(self.run_id))["status"], "running")
        self.kitaru.finish(self.run_id)
        self.assertEqual((await self.executor.poll(self.run_id))["status"], "completed")

    async def test_a_recorded_run_that_is_gone_is_reported(self):
        del self.kitaru.runs[self.run_id]
        with self.assertRaises(RunGone):
            await self.executor.poll(self.run_id)

    async def test_results_hold_the_replays_verdicts_and_result_sessions(self):
        self.kitaru.finish(self.run_id, "red")
        evidence = await self.executor.fetch_results(self.run_id)
        self.assertEqual(len(evidence.replays), 2)
        self.assertEqual([e["passed"] for e in evidence.evaluations], [False, True])
        self.assertEqual(set(evidence.sessions), {r["result_session_id"] for r in evidence.replays})
        self.assertEqual({s["cost"] for s in evidence.sessions.values()}, {"0.04"})

    async def test_cancel_stops_a_running_run(self):
        await self.executor.cancel(self.run_id)
        self.assertEqual(self.kitaru.runs[self.run_id]["status"], "canceled")

    async def test_cancel_of_a_finished_or_missing_run_is_quiet(self):
        self.kitaru.finish(self.run_id)
        await self.executor.cancel(self.run_id)
        await self.executor.cancel("run-404")


if __name__ == "__main__":
    unittest.main()
