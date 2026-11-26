"""The Loop steps end to end: the v1 routes, the real KitaruLoopSteps, a fake Kitaru and a memory store.

Each test drives a Loop the way the Evalve MCP does: start it, poll its status, send a change, cancel.
Between calls the test plays the Kitaru workers by finishing runs in the fake.
"""

import copy
import json
import logging
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from evalve_backend import loop_rules
from fake_kitaru import AGENT_VERSION_ID, COHORT_VERSION_ID, FakeKitaru
from helpers import TOKEN, client
from memory_store import MemoryStore

LOOPS = "/evalve/v1/loops"
OWNER = {"Authorization": f"Bearer {TOKEN}"}
TEAMMATE = {"Authorization": "Bearer teammate-token"}
# The dashboard session of the Loop owner.
SESSION = {"Cookie": "evalve_session=session-token-value", "X-CSRF-Token": "csrf-token-value"}
ONLY_BEARER = {"detail": "Only a Bearer credential can start a Run. Use the Evalve MCP."}
ME = "/api/v1/accounts/me"
RUNS = r"/api/v1/experiments/[^/]+/runs"
START = {
    "name": "Support loop", "cohort_version_id": COHORT_VERSION_ID,
    "evaluators": [{"ref": "policy@1", "params": {"strict": True}}],
    "tool_policy": {"default": {"type": "history", "scope": "baseline", "on_miss": "fail"}}, "allowed_changes": ["system_prompt", "model"],
    "allowed_models": ["gpt-large"], "required_consecutive_greens": 2, "max_runs": 6, "budget_usd": "2.00",
    "control_prompt": "Answer the customer.",
}
CHANGE = {"experiment": 1, "change": "Name the refund rule", "system_prompt": "Answer the customer. Cite the refund rule."}


class StepsTest(unittest.TestCase):
    def setUp(self):
        self.kitaru = FakeKitaru(OWNER["Authorization"])
        self.kitaru.accounts[TEAMMATE["Authorization"]] = {"id": "account-2", "name": "teammate", "active": True}
        owner = self.kitaru.accounts[OWNER["Authorization"]]
        self.kitaru.dashboard_sessions[SESSION["Cookie"], SESSION["X-CSRF-Token"]] = owner
        self.store = MemoryStore()
        self.api = self.enterContext(client(self.kitaru, store=self.store))

    # Calls of the Evalve MCP.

    def start(self, settings=None, *, key="start"):
        return self.api.post(LOOPS, headers=OWNER | {"Idempotency-Key": key}, json=START | (settings or {}))

    def started(self, **settings) -> str:
        reply = self.start(settings)
        self.assertEqual(reply.status_code, 201, reply.text)
        return reply.json()["loop_id"]

    def status(self, loop_id, headers=OWNER) -> dict:
        reply = self.api.get(f"{LOOPS}/{loop_id}", headers=headers)
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    def propose(self, loop_id, body=CHANGE):
        return self.api.post(f"{LOOPS}/{loop_id}/experiments", headers=OWNER, json=body)

    # The Kitaru workers.

    def latest_run(self) -> str:
        return list(self.kitaru.runs)[-1]

    def finish(self, outcome="green", cost="0.04"):
        self.kitaru.finish(self.latest_run(), outcome, cost)

    def stored(self, loop_id) -> dict:
        return self.store.get_loop(None, loop_id)

    def outcomes(self, loop_id) -> list[list[str | None]]:
        return [[(run["result"] or {}).get("outcome") for run in experiment["runs"]]
                for experiment in self.stored(loop_id)["experiments"]]

    def published(self, loop_id) -> dict:
        experiment_id = self.stored(loop_id)["experiments"][0]["experiment_id"]
        line = self.kitaru.experiments[experiment_id]["description"].splitlines()[-1]
        self.assertTrue(line.startswith(loop_rules.RESULT_PREFIX))
        return json.loads(line[len(loop_rules.RESULT_PREFIX):])

    def end(self, loop_id) -> tuple:
        loop = self.stored(loop_id)
        return loop["status"], loop["outcome"]


class DriveAndRefreshTest(StepsTest):
    """Only a Bearer credential of the Loop owner drives a Loop. A dashboard session refreshes it."""

    def kitaru_calls_since(self, mark: int) -> set[str]:
        return {request.url.path for request in self.kitaru.requests[mark:]}

    def test_a_dashboard_session_cannot_start_a_loop(self):
        reply = self.api.post(LOOPS, headers=SESSION | {"Idempotency-Key": "start"}, json=START)
        self.assertEqual((reply.status_code, reply.json()), (403, ONLY_BEARER))
        self.assertEqual(self.kitaru_calls_since(0), {ME})
        self.assertEqual(self.store.list_loops(None), [])

    def test_a_dashboard_session_cannot_propose_an_experiment(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        before, mark = copy.deepcopy(self.stored(loop_id)), len(self.kitaru.requests)
        reply = self.api.post(f"{LOOPS}/{loop_id}/experiments", headers=SESSION, json=CHANGE)
        self.assertEqual((reply.status_code, reply.json()), (403, ONLY_BEARER))
        self.assertEqual(self.kitaru_calls_since(mark), {ME})
        self.assertEqual(self.stored(loop_id), before)
        # The same change with a Bearer credential starts Experiment 1, as before.
        self.assertEqual(self.propose(loop_id).status_code, 201)
        self.assertEqual(len(self.kitaru.runs), 2)

    def test_a_refresh_records_a_finished_run_and_the_next_drive_starts_run_2(self):
        loop_id = self.started(required_consecutive_greens=3)
        self.finish("green")
        detail = self.status(loop_id, SESSION)
        self.assertEqual((detail["status"], detail["next"]["action"]), ("running", "replay"))
        self.assertEqual(self.outcomes(loop_id), [["green"]])
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)
        # A second refresh has nothing to record, and it starts nothing.
        self.assertEqual(self.status(loop_id, SESSION)["next"]["action"], "replay")
        self.assertEqual(self.outcomes(loop_id), [["green"]])
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)
        # The Loop waits for a driving call.
        self.assertEqual(self.status(loop_id)["next"]["action"], "collect")
        self.assertEqual(self.outcomes(loop_id), [["green", None]])
        self.assertEqual(len(self.kitaru.runs), 2)

    def test_a_refresh_of_a_run_that_is_not_finished_changes_nothing(self):
        loop_id = self.started()
        before = copy.deepcopy(self.stored(loop_id))
        self.assertEqual(self.status(loop_id, SESSION)["next"]["action"], "collect")
        self.assertEqual(self.stored(loop_id), before)

    def test_a_refresh_ends_a_loop_whose_last_result_makes_it_pass(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        self.assertEqual(self.propose(loop_id).status_code, 201)
        self.finish("green")
        self.status(loop_id)
        self.finish("green")
        detail = self.status(loop_id, SESSION)
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "passed"))
        self.assertEqual(self.outcomes(loop_id), [["red"], ["green", "green"]])
        self.assertEqual(self.published(loop_id)["status"], "stop_success")

    def test_a_refresh_does_not_send_a_start_that_never_reached_kitaru(self):
        self.kitaru.fail("POST", RUNS, 503)
        self.assertEqual(self.start().status_code, 503)
        (loop_id,) = self.store._loops
        self.assertEqual(self.status(loop_id, SESSION)["next"]["action"], "resume_start")
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)
        self.assertEqual(self.kitaru.runs, {})
        self.status(loop_id)
        self.assertEqual(len(self.kitaru.runs), 1)

    def test_a_refresh_does_not_retry_an_incomplete_run(self):
        loop_id = self.started()
        self.finish("incomplete")
        for _ in range(2):
            self.assertEqual(self.status(loop_id, SESSION)["next"]["action"], "retry")
        self.assertEqual(self.outcomes(loop_id), [["incomplete"]])
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)
        self.status(loop_id)
        self.assertEqual(self.outcomes(loop_id), [["incomplete", None]])

    def test_a_refresh_after_the_wait_limit_ends_the_loop_as_stopped_idle(self):
        loop_id = self.started()
        self.finish("red")
        self.assertEqual(self.status(loop_id, SESSION)["next"]["action"], "needs_change")
        self.store._loops[loop_id]["waiting_since"] -= timedelta(hours=25)
        detail = self.status(loop_id, SESSION)
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "stopped_idle"))

    def test_a_dashboard_session_cancels_a_loop_and_its_run(self):
        loop_id = self.started()
        reply = self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=SESSION)
        self.assertEqual((reply.status_code, reply.json()["status"]), (200, "cancelled"))
        self.assertEqual(self.kitaru.runs[self.latest_run()]["status"], "canceled")

    def test_a_request_with_an_authorization_header_and_the_session_cookie_drives(self):
        loop_id = self.started(required_consecutive_greens=3)
        self.finish("green")
        self.status(loop_id, OWNER | SESSION)
        self.assertEqual(self.outcomes(loop_id), [["green", None]])


class PassingTest(StepsTest):
    def test_a_green_control_experiment_is_already_passing(self):
        loop_id = self.started()
        self.finish("green")
        self.assertEqual(self.status(loop_id)["next"]["action"], "collect")
        self.assertEqual(len(self.kitaru.runs), 2)
        self.finish("green")
        detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "already_passing"))
        self.assertEqual(self.outcomes(loop_id), [["green", "green"]])
        self.assertEqual((self.published(loop_id)["status"], self.published(loop_id)["winner_lap"]), ("stop_success", 0))

    def test_the_first_red_control_run_asks_for_a_change(self):
        loop_id = self.started()
        self.finish("red")
        detail = self.status(loop_id)
        self.assertEqual((detail["next"]["action"], detail["next"]["experiment"]), ("needs_change", 1))
        self.assertEqual([case["case"] for case in detail["needs"]["failed_cases"]], ["Case 1"])
        # The agent reads what it answered before it proposes a change.
        self.assertEqual([case["reply"] for case in detail["needs"]["failed_cases"]], ["reply 0"])
        self.assertIsNotNone(self.stored(loop_id)["waiting_since"])
        self.assertEqual(len(self.kitaru.runs), 1)
        self.assertEqual(self.published(loop_id)["status"], "running")

    def test_a_change_starts_experiment_1_and_a_green_experiment_1_passes(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        reply = self.propose(loop_id)
        self.assertEqual(reply.status_code, 201, reply.text)
        experiment = self.kitaru.experiments[reply.json()["experiment_id"]]
        self.assertEqual(experiment["name"], f"evalve-{loop_id}-exp-1")
        self.assertEqual(experiment["override"]["system_prompt"], CHANGE["system_prompt"])
        self.assertIsNone(self.stored(loop_id)["waiting_since"])
        self.finish("green")
        self.status(loop_id)
        self.finish("green")
        detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "passed"))
        self.assertEqual(detail["winner"]["experiment"], 1)
        self.assertEqual(self.outcomes(loop_id), [["red"], ["green", "green"]])

    def test_green_incomplete_green_passes_with_a_streak_of_2(self):
        loop_id = self.started()
        for outcome in ("green", "incomplete", "green"):
            self.finish(outcome)
            detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "already_passing"))
        self.assertEqual(self.outcomes(loop_id), [["green", "incomplete", "green"]])


class RestartSafetyTest(StepsTest):
    def test_a_lost_submit_reply_is_retried_with_the_same_key(self):
        self.kitaru.lose_next_reply("POST", RUNS)
        self.assertEqual(self.start().status_code, 503)
        # Inside the key window the backend sends the same request again, and Kitaru answers with the run.
        reply = self.start()
        self.assertEqual(reply.status_code, 200, reply.text)
        first, second = self.kitaru.requests_to("POST", RUNS)
        self.assertEqual((first.headers["idempotency-key"], first.content), (second.headers["idempotency-key"], second.content))
        self.assertEqual(len(self.kitaru.runs), 1)

    def submit_started_ago(self, minutes):
        (loop,) = self.store._loops.values()
        run = loop["experiments"][-1]["runs"][-1]
        run["submit_started_at"] -= timedelta(minutes=minutes)
        if run["start_sent_at"] is not None:
            run["start_sent_at"] -= timedelta(minutes=minutes)

    def submit_started_16_minutes_ago(self):
        self.submit_started_ago(16)
        self.kitaru.expire_keys()

    def cancel(self):
        (loop_id,) = self.store._loops
        return self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER)

    def test_a_call_after_the_key_expired_adopts_the_run_and_starts_no_second_run(self):
        self.kitaru.lose_next_reply("POST", RUNS)
        self.assertEqual(self.start().status_code, 503)
        self.submit_started_16_minutes_ago()
        reply = self.start()
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)
        (run_id,) = self.kitaru.runs
        self.assertEqual(self.stored(reply.json()["loop_id"])["experiments"][0]["runs"][0]["run_id"], run_id)

    def test_after_the_key_expired_two_runs_that_fit_fail_the_loop_instead_of_guessing(self):
        self.kitaru.lose_next_reply("POST", RUNS)
        self.assertEqual(self.start().status_code, 503)
        (ours,) = self.kitaru.runs
        self.kitaru.add_run(self.kitaru.runs[ours]["experiment_id"])
        self.submit_started_16_minutes_ago()
        reply = self.start()
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.end(reply.json()["loop_id"]), ("failed", "error"))
        self.assertIn("cannot tell which is its own", self.stored(reply.json()["loop_id"])["reason"])
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)

    def test_a_cancel_after_a_start_that_never_reached_kitaru_starts_nothing(self):
        self.kitaru.fail("POST", RUNS, 503)
        self.assertEqual(self.start().status_code, 503)
        # One empty look right after the start proves nothing, so the cancel waits.
        self.assertEqual(self.cancel().status_code, 503)
        self.submit_started_ago(3)
        self.assertEqual(self.cancel().json()["status"], "cancelled")
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)
        self.assertEqual(self.kitaru.runs, {})

    def test_a_cancel_waits_for_a_retried_start_that_timed_out(self):
        self.kitaru.fail("POST", RUNS, 503)
        self.assertEqual(self.start().status_code, 503)
        self.submit_started_ago(5)
        # A status call five minutes later sends the start again. It times out, but Kitaru creates the run late.
        self.kitaru.fail("POST", RUNS, 504)
        (loop_id,) = self.store._loops
        self.api.get(f"{LOOPS}/{loop_id}", headers=OWNER)
        self.assertEqual(self.cancel().status_code, 503)
        late = self.kitaru.add_run(self.stored(loop_id)["experiments"][0]["experiment_id"])["id"]
        self.assertEqual(self.cancel().json()["status"], "cancelled")
        self.assertEqual(self.kitaru.runs[late]["status"], "canceled")

    def test_a_cancel_stops_a_run_that_a_timed_out_start_creates_late(self):
        self.kitaru.fail("POST", RUNS, 504)
        self.assertEqual(self.start().status_code, 503)
        self.assertEqual(self.cancel().status_code, 503)
        (loop,) = self.store._loops.values()
        # Kitaru finishes the first request after the backend stopped waiting.
        late = self.kitaru.add_run(loop["experiments"][0]["experiment_id"])["id"]
        self.assertEqual(self.cancel().json()["status"], "cancelled")
        self.assertEqual(self.kitaru.runs[late]["status"], "canceled")

    def test_a_cancel_after_a_lost_start_reply_stops_the_run_without_sending_the_start_again(self):
        self.kitaru.lose_next_reply("POST", RUNS)
        self.assertEqual(self.start().status_code, 503)
        (ours,) = self.kitaru.runs
        (loop_id,) = self.store._loops
        self.assertEqual(self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER).json()["status"], "cancelled")
        self.assertEqual(self.kitaru.runs[ours]["status"], "canceled")
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 1)

    def test_a_cancel_after_the_key_expired_never_cancels_a_run_it_cannot_tell_apart(self):
        self.kitaru.lose_next_reply("POST", RUNS)
        self.assertEqual(self.start().status_code, 503)
        (ours,) = self.kitaru.runs
        theirs = self.kitaru.add_run(self.kitaru.runs[ours]["experiment_id"])["id"]
        self.submit_started_16_minutes_ago()
        (loop_id,) = self.store._loops
        self.assertEqual(self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER).json()["status"], "cancelled")
        self.assertEqual(self.kitaru.runs[theirs]["status"], "running")

    def test_a_run_that_the_backend_did_not_start_is_ignored(self):
        loop_id = self.started()
        ours = self.latest_run()
        experiment_id = self.stored(loop_id)["experiments"][0]["experiment_id"]
        stranger = self.kitaru.add_run(experiment_id, agent_version_id="agent-version-1")["id"]
        self.kitaru.finish(stranger, "red", cost="9.00")
        self.kitaru.runs[stranger]["status"] = "running"
        self.kitaru.finish(ours, "green")
        self.status(loop_id)
        self.assertEqual(self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER).status_code, 200)
        self.assertEqual(self.kitaru.runs[stranger]["status"], "running")
        run_ids = {run["run_id"] for run in self.stored(loop_id)["experiments"][0]["runs"]}
        self.assertNotIn(stranger, run_ids)
        self.assertEqual(self.status(loop_id)["spend"]["recorded_usd"], "0.08")

    def test_a_recorded_run_that_is_gone_counts_as_incomplete(self):
        loop_id = self.started()
        del self.kitaru.runs[self.latest_run()]
        detail = self.status(loop_id)
        self.assertEqual(self.outcomes(loop_id), [["incomplete", None]])
        self.assertEqual(detail["experiments"][0]["runs"][0]["result"]["run_status"], "missing")
        self.assertEqual(len(self.kitaru.requests_to("POST", RUNS)), 2)

    def test_a_changed_run_spec_fails_the_loop(self):
        loop_id = self.started()
        self.kitaru.agent_versions[AGENT_VERSION_ID]["run_spec"]["env"]["OPENAI_MODEL"] = "gpt-other"
        self.finish("green")
        detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("failed", "error"))
        self.assertIn("run spec changed", detail["reason"])
        self.assertEqual(len(self.kitaru.runs), 1)
        self.assertEqual(self.published(loop_id)["status"], "error")

    def test_an_experiment_that_someone_changed_fails_the_loop(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        self.propose(loop_id)
        experiment_id = self.stored(loop_id)["experiments"][1]["experiment_id"]
        self.kitaru.experiments[experiment_id]["tool_policy"] = {"default": {"type": "passthrough"}}
        self.finish("green")
        detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("failed", "error"))
        self.assertIn("no longer matches", detail["reason"])


class CancelTest(StepsTest):
    def test_cancel_during_a_run_stops_it(self):
        loop_id = self.started()
        run_id = self.latest_run()
        reply = self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual((reply.json()["status"], reply.json()["outcome"]), ("cancelled", "stopped_by_user"))
        self.assertEqual(self.kitaru.runs[run_id]["status"], "canceled")
        self.assertEqual(self.published(loop_id)["status"], "cancelled")

    def test_cancel_while_waiting_for_a_change_calls_no_run(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        self.kitaru.requests.clear()
        self.assertEqual(self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER).json()["status"], "cancelled")
        self.assertEqual(self.kitaru.requests_to("POST", r"/api/v1/experiment-runs/[^/]+/cancel"), [])


class IdleLimitTest(StepsTest):
    def waiting(self, hours) -> str:
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        self.store._loops[loop_id]["waiting_since"] -= timedelta(hours=hours)
        return loop_id

    def test_a_status_call_after_24_hours_stops_the_loop(self):
        detail = self.status(self.waiting(25))
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "stopped_idle"))

    def test_a_change_after_the_limit_is_refused_and_starts_no_run(self):
        loop_id = self.waiting(25)
        self.assertEqual(self.propose(loop_id).status_code, 409)
        self.assertEqual(self.end(loop_id), ("completed", "stopped_idle"))
        self.assertEqual(len(self.kitaru.runs), 1)
        self.assertEqual(len(self.kitaru.experiments), 1)

    def test_a_change_before_the_limit_clears_the_wait(self):
        loop_id = self.waiting(23)
        self.assertEqual(self.propose(loop_id).status_code, 201)
        self.assertIsNone(self.stored(loop_id)["waiting_since"])
        self.assertEqual(len(self.kitaru.runs), 2)


class SafetyLimitTest(StepsTest):
    def test_two_incomplete_runs_in_a_row_fail_the_loop_and_start_no_third(self):
        loop_id = self.started()
        self.finish("incomplete")
        self.status(loop_id)
        self.finish("incomplete")
        detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("failed", "error"))
        self.assertEqual(detail["reason"], "Two Runs in a row could not be scored.")
        self.assertEqual(len(self.kitaru.runs), 2)

    def test_a_run_that_would_exceed_the_budget_does_not_start(self):
        loop_id = self.started(budget_usd="0.15")
        self.finish("green")
        detail = self.status(loop_id)
        self.assertEqual((detail["status"], detail["outcome"]), ("completed", "budget_reached"))
        self.assertIn("exceed the budget", detail["reason"])
        self.assertEqual(len(self.kitaru.runs), 1)
        self.assertEqual(self.published(loop_id)["status"], "stop_budget")

    def test_a_control_run_over_the_budget_is_refused_at_start(self):
        reply = self.start({"budget_usd": "0.05"})
        self.assertEqual(reply.status_code, 422)
        self.assertIn("exceed the budget", reply.json()["detail"])
        self.assertEqual(self.kitaru.runs, {})

    def test_a_run_with_a_null_cost_is_accounted_at_its_estimate(self):
        loop_id = self.started()
        self.finish("green", cost=None)
        spend = self.status(loop_id)["spend"]
        self.assertEqual((Decimal(spend["recorded_usd"]), Decimal(spend["accounted_usd"])), (Decimal(0), Decimal("0.10")))


class KeyAndOwnerTest(StepsTest):
    def test_every_kitaru_request_uses_the_callers_key_and_the_key_is_never_kept(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("evalve_backend").debug("Capture every evalve_backend log line.")
            loop_id = self.started()
            self.kitaru.fail("GET", r"/api/v1/experiment-runs/[^/]+", 500)
            self.assertEqual(self.api.get(f"{LOOPS}/{loop_id}", headers=OWNER).status_code, 503)
            self.finish("red")
            self.status(loop_id)
            self.propose(loop_id)
            self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=OWNER)
        self.assertEqual({r.headers["authorization"] for r in self.kitaru.requests}, {OWNER["Authorization"]})
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        self.assertNotIn(TOKEN, json.dumps(self.store._loops, default=str))

    def test_two_status_calls_at_the_same_time_start_one_run(self):
        loop_id = self.started()
        self.finish("green")
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.api.get(f"{LOOPS}/{loop_id}", headers=OWNER), range(2)))
        self.assertEqual({reply.status_code for reply in replies}, {200})
        self.assertEqual(len(self.kitaru.runs), 2)
        self.assertEqual(len(self.stored(loop_id)["experiments"][0]["runs"]), 2)

    def test_a_status_call_while_another_call_holds_the_lock_changes_nothing(self):
        loop_id = self.started()
        self.finish("green")
        lock = self.store.lock_loop(loop_id)
        self.assertEqual(self.status(loop_id)["next"]["action"], "collect")
        self.assertEqual(self.outcomes(loop_id), [[None]])
        lock.release()
        self.status(loop_id)
        self.assertEqual(self.outcomes(loop_id), [["green", None]])

    def test_a_teammate_status_call_moves_nothing(self):
        loop_id = self.started()
        self.finish("green")
        self.kitaru.requests.clear()
        detail = self.status(loop_id, TEAMMATE)
        self.assertEqual((detail["status"], self.outcomes(loop_id)), ("running", [[None]]))
        self.assertEqual([r.url.path for r in self.kitaru.requests], ["/api/v1/accounts/me"])

    def test_a_teammate_status_call_does_not_apply_the_idle_limit(self):
        loop_id = self.started()
        self.finish("red")
        self.status(loop_id)
        self.store._loops[loop_id]["waiting_since"] -= timedelta(hours=30)
        self.assertEqual(self.status(loop_id, TEAMMATE)["status"], "running")
        self.assertEqual(self.end(loop_id), ("running", None))


if __name__ == "__main__":
    unittest.main()
