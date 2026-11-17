"""The five cases of evalve-ui/tests/test_evalve_loop.py, run against the pure Loop rules.

The old tests ran the whole script against a fake Kitaru. These write down what happened
(the `laps[].attempts[].result` state) and check the decision.
"""

import copy
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


COHORT_VERSION = "00000000-0000-4000-8000-000000000001"


def form(**changes) -> dict:
    return {
        "name": "Test loop", "server_url": "https://kitaru.example.test/", "cohort_version_id": COHORT_VERSION,
        "evaluators": [{"ref": "policy@1"}], "allowed_changes": ["system_prompt"],
        "tool_policy": {"default": {"type": "history", "scope": "baseline", "on_miss": "fail"}},
        "required_consecutive_greens": 2, "max_attempts": 10, "budget_usd": "1.00",
    } | changes


class ValidateConfigTest(unittest.TestCase):
    def test_a_valid_form_is_normalized_without_changing_the_input(self):
        original = form(budget_usd=1.5, cohort_version_id=COHORT_VERSION.upper())
        before = copy.deepcopy(original)
        cfg = loop_rules.validate_config(original)
        self.assertEqual(original, before)
        self.assertEqual(cfg["budget_usd"], "1.5")
        self.assertEqual(cfg["cohort_version_id"], COHORT_VERSION)
        self.assertEqual(cfg["server_url"], "https://kitaru.example.test")
        self.assertEqual((cfg["allowed_models"], cfg["tool_policy"]["tools"]), ([], {}))

    def test_a_missing_field_is_named(self):
        bad = form()
        del bad["budget_usd"]
        with self.assertRaisesRegex(loop_rules.ConfigError, "budget_usd"):
            loop_rules.validate_config(bad)

    def test_bad_settings_are_refused(self):
        cases = {
            "name": form(name="!!!"),
            "cohort": form(cohort_version_id="not-a-uuid"),
            "server": form(server_url="https://user:pass@kitaru"),
            "streak above the cap": form(required_consecutive_greens=3, max_attempts=2),
            "streak is not an integer": form(required_consecutive_greens=True),
            "zero budget": form(budget_usd="0"),
            "negative budget": form(budget_usd="-1"),
            "budget is not a number": form(budget_usd="lots"),
            "no allowed change": form(allowed_changes=[]),
            "unknown change": form(allowed_changes=["temperature"]),
            "model change without models": form(allowed_changes=["model"]),
            "blank model": form(allowed_changes=["model"], allowed_models=[" "]),
            "no evaluators": form(evaluators=[]),
            "evaluator without a version": form(evaluators=[{"ref": "policy"}]),
            "repeated evaluator": form(evaluators=[{"ref": "policy@1"}, {"ref": "policy@1"}]),
            "no tool policy default": form(tool_policy={}),
            "tool policy without a type": form(tool_policy={"default": {}}),
            "blank baseline model": form(baseline_model=" "),
            "bad agent version": form(agent_version="agent"),
        }
        for label, bad in cases.items():
            with self.subTest(label):
                with self.assertRaises(loop_rules.ConfigError):
                    loop_rules.validate_config(bad)


class ValidateCandidateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = loop_rules.validate_config(form(allowed_changes=["system_prompt", "model"], allowed_models=["model-b", "model-c"]))
        self.state = new_state() | {"baseline_prompt": "Be brief."}
        add_attempt(self.state, "red")

    def refused(self, override, change="Try something", baseline_model=None, cfg=None):
        with self.assertRaises(loop_rules.CandidateError):
            loop_rules.validate_candidate(cfg or self.cfg, self.state, override, change, baseline_model)

    def test_a_new_prompt_is_accepted(self):
        plan = loop_rules.validate_candidate(self.cfg, self.state, {"system_prompt": "Be kind."}, " Kinder tone ", None)
        self.assertEqual(plan, {"override": {"system_prompt": "Be kind."}, "change": "Kinder tone", "baseline_model": BASELINE_MODEL})

    def test_a_new_allowed_model_is_accepted(self):
        plan = loop_rules.validate_candidate(self.cfg, self.state, {"model": "model-b"}, "Bigger model", None)
        self.assertEqual((plan["override"], plan["baseline_model"]), ({"model": "model-b"}, BASELINE_MODEL))

    def test_the_inherited_model_is_pinned_the_first_time_it_is_supplied(self):
        self.state["baseline_model"] = None
        plan = loop_rules.validate_candidate(self.cfg, self.state, {"model": "model-b"}, "Bigger model", "model-a")
        self.assertEqual(plan["baseline_model"], "model-a")
        self.refused({"model": "model-b"})
        self.refused({"model": "model-b"}, baseline_model=" ")

    def test_a_repeated_override_is_refused(self):
        self.state["laps"].append({"number": 1, "override": {"model": "model-b"}, "change": "x", "attempts": []})
        self.state["laps"].append({"number": 2, "override": {"system_prompt": "Be kind."}, "change": "y", "attempts": []})
        self.refused({"model": "model-b"})
        self.refused({"system_prompt": "Be kind."})
        loop_rules.validate_candidate(self.cfg, self.state, {"model": "model-c"}, "Another model", None)

    def test_a_model_that_is_not_allowed_is_refused(self):
        self.refused({"model": "model-z"})
        self.refused({"model": "model-b"}, cfg=loop_rules.validate_config(form()))

    def test_the_baseline_model_is_refused(self):
        self.cfg["allowed_models"].append(BASELINE_MODEL)
        self.refused({"model": BASELINE_MODEL})

    def test_a_pinned_baseline_model_cannot_be_replaced(self):
        self.refused({"model": "model-b"}, baseline_model="other-model")

    def test_a_prompt_that_does_not_change_the_baseline_is_refused(self):
        self.refused({"system_prompt": "Be brief."})
        self.refused({"system_prompt": "  "})

    def test_prompt_changes_must_be_allowed_and_take_no_baseline_model(self):
        self.refused({"system_prompt": "Be kind."}, cfg=loop_rules.validate_config(form(allowed_changes=["model"], allowed_models=["model-b"])))
        self.refused({"system_prompt": "Be kind."}, baseline_model=BASELINE_MODEL)

    def test_a_change_summary_and_exactly_one_override_are_required(self):
        self.refused({"system_prompt": "Be kind."}, change=" ")
        self.refused({})
        self.refused({"model": "model-b", "system_prompt": "Be kind."})


if __name__ == "__main__":
    unittest.main()
