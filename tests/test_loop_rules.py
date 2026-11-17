"""The five cases of evalve-ui/tests/test_evalve_loop.py, run against the pure Loop rules.

The old tests ran the whole script against a fake Kitaru. These write down what happened
(the `laps[].attempts[].result` state) and check the decision.
"""

import unittest
from decimal import Decimal

from evalve_backend import loop_rules

BASELINE_MODEL = "baseline-model"


def config(**limits) -> dict:
    return {"required_consecutive_greens": 2, "max_attempts": 10, "budget_usd": "1.00"} | limits


def new_state() -> dict:
    return {
        "loop_id": "loop-1", "baseline_model": BASELINE_MODEL, "budget_stop": None,
        "preflight": {"baseline_estimate_usd": "0.10"}, "laps": [],
    }


def result(outcome: str, cost: str = "0.10", cost_incomplete: bool = False) -> dict:
    return {"outcome": outcome, "recorded_cost_usd": cost, "cost_incomplete": cost_incomplete}


def add_attempt(state: dict, outcome: str | None, run_id: str | None = "run", **cost) -> dict:
    """Add one attempt to the baseline lap. An outcome of None is a submission with no result yet."""
    if not state["laps"]:
        state["laps"].append({"number": 0, "override": {}, "change": "Unchanged baseline", "attempts": []})
    attempts = state["laps"][-1]["attempts"]
    attempt = {
        "number": len(attempts) + 1, "run_id": run_id, "estimated_cost_usd": "0.10",
        "result": result(outcome, **cost) if outcome else None,
    }
    attempts.append(attempt)
    return attempt


class LoopRulesTest(unittest.TestCase):
    def next_action(self, cfg: dict, state: dict, outcome: str | None) -> dict:
        add_attempt(state, outcome)
        return loop_rules.decide(cfg, state)

    def test_red_baseline_still_requires_two_fully_scored_runs(self):
        cfg, state = config(), new_state()
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "start_baseline")
        self.assertEqual(self.next_action(cfg, state, "red")["action"], "replay")
        self.assertEqual(self.next_action(cfg, state, "incomplete")["action"], "retry")
        self.assertEqual(self.next_action(cfg, state, "green")["action"], "start_candidate")

    def test_incomplete_baseline_breaks_the_green_streak(self):
        cfg, state = config(), new_state()
        self.assertEqual(self.next_action(cfg, state, "green")["action"], "replay")
        step = self.next_action(cfg, state, "incomplete")
        self.assertEqual((step["action"], step["green_streak"]), ("retry", 0))
        self.assertEqual(self.next_action(cfg, state, "green")["action"], "replay")
        step = self.next_action(cfg, state, "green")
        self.assertEqual((step["action"], step["lap"]), ("stop_success", 0))

    def test_budget_blocks_next_submission_using_estimated_missing_cost(self):
        cfg, state = config(budget_usd="0.19"), new_state()
        add_attempt(state, "incomplete", cost="0", cost_incomplete=True)
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["accounted_cost_usd"]), ("retry", "0.10"))

        estimate = loop_rules.estimate_attempt(state, {})
        self.assertEqual(estimate, Decimal("0.10"))
        plan = loop_rules.plan_submission(cfg, state, step, estimate)
        self.assertIn("budget", plan["budget_stop"])

        # The caller saves the reason. Deciding again keeps stopping and submits nothing.
        state["budget_stop"] = plan["budget_stop"]
        for _ in range(2):
            self.assertEqual(loop_rules.decide(cfg, state)["action"], "stop_budget")
        self.assertEqual(len(state["laps"][0]["attempts"]), 1)

    def test_attempt_cap_counts_incomplete_runs(self):
        cfg, state = config(max_attempts=2), new_state()
        self.assertEqual(self.next_action(cfg, state, "red")["action"], "replay")
        self.assertEqual(self.next_action(cfg, state, "incomplete")["action"], "stop_attempt_cap")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "stop_attempt_cap")
        self.assertEqual(len(state["laps"][0]["attempts"]), 2)

    def test_interrupted_submission_resumes_with_the_same_attempt(self):
        cfg, state = config(), new_state()
        attempt = add_attempt(state, None, run_id=None)
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "resume_start")

        attempt["run_id"] = "run-1"
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["run_id"], step["attempts"]), ("collect", "run-1", 1))


if __name__ == "__main__":
    unittest.main()
