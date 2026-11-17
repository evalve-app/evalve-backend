"""The five cases of evalve-ui/tests/test_evalve_loop.py, run against the pure Loop rules.

The old tests ran the whole script against a fake Kitaru. These write down what happened
(the `experiments[].runs[].result` state) and check the decision. Two of the five cases expect
a different result than in `loop.py`, because of the rules decided in sc-327.
"""

import ast
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from evalve_backend import loop_rules

CONTROL_MODEL = "control-model"


def config(**limits) -> dict:
    return {"required_consecutive_greens": 2, "max_runs": 10, "budget_usd": "1.00"} | limits


def new_state() -> dict:
    return {
        "loop_id": "loop-1", "control_model": CONTROL_MODEL, "budget_stop": None,
        "preflight": {"control_estimate_usd": "0.10"}, "experiments": [],
    }


def result(outcome: str, cost: str = "0.10", cost_incomplete: bool = False) -> dict:
    return {"outcome": outcome, "recorded_cost_usd": cost, "cost_incomplete": cost_incomplete}


def add_run(state: dict, outcome: str | None, run_id: str | None = "run", **cost) -> dict:
    """Add one Run to the last Experiment (the Control Experiment when there is none).

    An outcome of None is a submission with no result yet.
    """
    if not state["experiments"]:
        state["experiments"].append({"number": 0, "override": {}, "change": "Unchanged control", "runs": []})
    runs = state["experiments"][-1]["runs"]
    run = {
        "number": len(runs) + 1, "run_id": run_id, "estimated_cost_usd": "0.10",
        "result": result(outcome, **cost) if outcome else None,
    }
    runs.append(run)
    return run


def add_experiment(state: dict, override: dict, *outcomes: str) -> dict:
    """Start the next Experiment with a Run for each outcome."""
    number = len(state["experiments"])
    experiment = {"number": number, "override": override, "change": f"Change {number}", "runs": []}
    state["experiments"].append(experiment)
    for outcome in outcomes:
        add_run(state, outcome)
    return experiment


class LoopRulesTest(unittest.TestCase):
    def next_action(self, cfg: dict, state: dict, outcome: str | None) -> dict:
        add_run(state, outcome)
        return loop_rules.decide(cfg, state)

    def test_red_control_experiment_run_ends_it(self):
        # In loop.py the control lap continued until it had two scored runs, even after a red one.
        cfg, state = config(), new_state()
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "start_control")
        step = self.next_action(cfg, state, "red")
        self.assertEqual((step["action"], step["experiment"]), ("needs_change", 1))

    def test_incomplete_run_does_not_break_the_green_streak(self):
        # In loop.py an incomplete run started the streak again, so the second green was not enough.
        cfg, state = config(), new_state()
        self.assertEqual(self.next_action(cfg, state, "green")["action"], "replay")
        step = self.next_action(cfg, state, "incomplete")
        self.assertEqual((step["action"], step["green_streak"]), ("retry", 1))
        step = self.next_action(cfg, state, "green")
        self.assertEqual((step["action"], step["experiment"], step["green_streak"]), ("stop_success", 0, 2))

    def test_budget_blocks_next_submission_using_estimated_missing_cost(self):
        cfg, state = config(budget_usd="0.19"), new_state()
        add_run(state, "incomplete", cost="0", cost_incomplete=True)
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["accounted_cost_usd"]), ("retry", "0.10"))

        estimate = loop_rules.estimate_run(state, {})
        self.assertEqual(estimate, Decimal("0.10"))
        plan = loop_rules.plan_submission(cfg, state, step, estimate)
        self.assertIn("budget", plan["budget_stop"])

        # The caller saves the reason. Deciding again keeps stopping and submits nothing.
        state["budget_stop"] = plan["budget_stop"]
        for _ in range(2):
            self.assertEqual(loop_rules.decide(cfg, state)["action"], "stop_budget")
        self.assertEqual(len(state["experiments"][0]["runs"]), 1)

    def test_run_cap_counts_incomplete_runs(self):
        # loop.py used a red first run here. A red run now ends the Experiment, so the cap is reached
        # by an incomplete run instead.
        cfg, state = config(max_runs=2), new_state()
        self.assertEqual(self.next_action(cfg, state, "green")["action"], "replay")
        self.assertEqual(self.next_action(cfg, state, "incomplete")["action"], "stop_attempt_cap")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "stop_attempt_cap")
        self.assertEqual(len(state["experiments"][0]["runs"]), 2)

    def test_interrupted_submission_resumes_with_the_same_run(self):
        cfg, state = config(), new_state()
        run = add_run(state, None, run_id=None)
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "resume_start")

        run["run_id"] = "run-1"
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["run_id"], step["runs"]), ("collect", "run-1", 1))


COHORT_VERSION = "00000000-0000-4000-8000-000000000001"


def form(**changes) -> dict:
    return {
        "name": "Test loop", "server_url": "https://kitaru.example.test/", "cohort_version_id": COHORT_VERSION,
        "evaluators": [{"ref": "policy@1"}], "allowed_changes": ["system_prompt"],
        "tool_policy": {"default": {"type": "history", "scope": "baseline", "on_miss": "fail"}},
        "required_consecutive_greens": 2, "max_runs": 10, "budget_usd": "1.00",
    } | changes


def is_count(value, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def parse_evalve_loop(text: str | None) -> dict | None:
    """The dashboard's reading of a description: parseEvalveLoop in evalve-ui/src/lib/loops.ts.

    A Python copy of its rules (descriptionRecord at :178, parseEvalveLoop at :190), kept as plain
    as the original so the two can be compared line by line. It cannot see later changes there.
    """
    def record(prefix):
        line = next((line for line in text.split("\n") if line.startswith(prefix)), None)
        if line is None:
            return None
        try:
            value = json.loads(line[len(prefix):])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def text_or_none(value):
        return value if isinstance(value, str) and value else None

    def text_list(value):
        return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else None

    if not text:
        return None
    settings = record("Evalve loop: ")
    if settings is None or settings.get("schema_version") != 1 or settings.get("lap") != 0 or not isinstance(settings.get("loop_id"), str):
        return None
    greens = settings.get("required_consecutive_greens")
    if not is_count(greens, 1):
        return None

    saved = record("Evalve result: ")
    result = None
    if saved and saved.get("schema_version") == 1 and saved.get("loop_id") == settings["loop_id"]:
        status = saved.get("status")
        if status == "stop_success":
            if is_count(saved.get("winner_lap"), 0):
                result = {"status": status, "winner_lap": saved["winner_lap"]}
        elif status in ("stop_attempt_cap", "stop_budget"):
            reason = saved.get("reason")
            result = {"status": status, "reason": reason if isinstance(reason, str) else None}
        elif status == "running":
            result = {"status": status}

    runs = []
    if result and result["status"] != "running" and isinstance(saved.get("laps"), list):
        for lap in saved["laps"]:
            if isinstance(lap, dict) and isinstance(lap.get("experiment_id"), str) and isinstance(lap.get("runs"), list):
                runs += [
                    {"experiment_id": lap["experiment_id"], "run_id": run["run_id"]}
                    for run in lap["runs"] if isinstance(run, dict) and isinstance(run.get("run_id"), str)
                ]
    max_attempts = settings.get("max_attempts")
    return {
        "required_greens": greens,
        "setup": {
            "name": text_or_none(settings.get("name")), "cohort_version_id": text_or_none(settings.get("cohort_version_id")),
            "agent_version_id": text_or_none(settings.get("agent_version_id")), "evaluators": text_list(settings.get("evaluators")),
            "allowed_changes": text_list(settings.get("allowed_changes")), "allowed_models": text_list(settings.get("allowed_models")),
            "required_greens": greens, "max_attempts": max_attempts if type(max_attempts) is int else None,
            "budget_usd": text_or_none(settings.get("budget_usd")),
        },
        "result": result,
        "published_runs": runs,
    }


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
            "streak above the cap": form(required_consecutive_greens=3, max_runs=2),
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
            "blank control model": form(control_model=" "),
            "bad agent version": form(agent_version="agent"),
        }
        for label, bad in cases.items():
            with self.subTest(label):
                with self.assertRaises(loop_rules.ConfigError):
                    loop_rules.validate_config(bad)


class ValidateCandidateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = loop_rules.validate_config(form(allowed_changes=["system_prompt", "model"], allowed_models=["model-b", "model-c"]))
        self.state = new_state() | {"control_prompt": "Be brief."}
        add_run(self.state, "red")

    def refused(self, override, change="Try something", control_model=None, cfg=None):
        with self.assertRaises(loop_rules.CandidateError):
            loop_rules.validate_candidate(cfg or self.cfg, self.state, override, change, control_model)

    def test_a_new_prompt_is_accepted(self):
        plan = loop_rules.validate_candidate(self.cfg, self.state, {"system_prompt": "Be kind."}, " Kinder tone ", None)
        self.assertEqual(plan, {"override": {"system_prompt": "Be kind."}, "change": "Kinder tone", "control_model": CONTROL_MODEL})

    def test_a_new_allowed_model_is_accepted(self):
        plan = loop_rules.validate_candidate(self.cfg, self.state, {"model": "model-b"}, "Bigger model", None)
        self.assertEqual((plan["override"], plan["control_model"]), ({"model": "model-b"}, CONTROL_MODEL))

    def test_the_inherited_model_is_pinned_the_first_time_it_is_supplied(self):
        self.state["control_model"] = None
        plan = loop_rules.validate_candidate(self.cfg, self.state, {"model": "model-b"}, "Bigger model", "model-a")
        self.assertEqual(plan["control_model"], "model-a")
        self.refused({"model": "model-b"})
        self.refused({"model": "model-b"}, control_model=" ")

    def test_a_repeated_override_is_refused(self):
        self.state["experiments"].append({"number": 1, "override": {"model": "model-b"}, "change": "x", "runs": []})
        self.state["experiments"].append({"number": 2, "override": {"system_prompt": "Be kind."}, "change": "y", "runs": []})
        self.refused({"model": "model-b"})
        self.refused({"system_prompt": "Be kind."})
        loop_rules.validate_candidate(self.cfg, self.state, {"model": "model-c"}, "Another model", None)

    def test_a_model_that_is_not_allowed_is_refused(self):
        self.refused({"model": "model-z"})
        self.refused({"model": "model-b"}, cfg=loop_rules.validate_config(form()))

    def test_the_control_model_is_refused(self):
        self.cfg["allowed_models"].append(CONTROL_MODEL)
        self.refused({"model": CONTROL_MODEL})

    def test_a_pinned_control_model_cannot_be_replaced(self):
        self.refused({"model": "model-b"}, control_model="other-model")

    def test_a_prompt_that_does_not_change_the_control_prompt_is_refused(self):
        self.refused({"system_prompt": "Be brief."})
        self.refused({"system_prompt": "  "})

    def test_prompt_changes_must_be_allowed_and_take_no_control_model(self):
        self.refused({"system_prompt": "Be kind."}, cfg=loop_rules.validate_config(form(allowed_changes=["model"], allowed_models=["model-b"])))
        self.refused({"system_prompt": "Be kind."}, control_model=CONTROL_MODEL)

    def test_a_value_of_the_wrong_type_is_refused_as_a_candidate_error(self):
        # A caller catches CandidateError. A value that is not text must not raise a different error.
        for prompt in (None, 5, ["Be kind."]):
            with self.subTest(prompt=prompt):
                self.refused({"system_prompt": prompt})
        for override in (None, ["model"], "model"):
            with self.subTest(override=override):
                self.refused(override)
        for model in (None, 5, ["model-b"]):
            with self.subTest(model=model):
                self.refused({"model": model})
        self.state["control_model"] = None
        self.refused({"model": "model-b"}, control_model=5)
        self.refused({"system_prompt": "Be kind."}, change=None)

    def test_a_change_summary_and_exactly_one_override_are_required(self):
        self.refused({"system_prompt": "Be kind."}, change=" ")
        self.refused({})
        self.refused({"model": "model-b", "system_prompt": "Be kind."})


class ScoreRunTest(unittest.TestCase):
    """A cohort of two sessions, scored by two evaluators."""

    def setUp(self):
        self.state = {
            "members": {"recorded-1": "Case 1", "recorded-2": "Case 2"},
            "evaluators": [
                {"version_id": "policy-v", "evaluator": "policy", "version": 1, "params": {}},
                {"version_id": "tone-v", "evaluator": "tone", "version": 2, "params": {"strict": True}},
            ],
        }
        self.run = {"id": "run-1", "status": "completed"}
        self.replays = [
            {"id": f"replay-{n}", "baseline_session_id": f"recorded-{n}", "result_session_id": f"result-{n}", "status": "completed"}
            for n in (1, 2)
        ]
        self.sessions = {f"result-{n}": {"status": "completed", "cost": "0.10"} for n in (1, 2)}
        self.evaluations = [self.verdict(n, e, True) for n in (1, 2) for e in ("policy", "tone")]

    def verdict(self, n, evaluator, passed):
        params = {"policy": {}, "tone": {"strict": True}}[evaluator]
        return {"id": f"{evaluator}-{n}", "session_id": f"result-{n}", "evaluator_version_id": f"{evaluator}-v",
                "evaluator_params": params, "passed": passed}

    def score(self):
        return loop_rules.score_run(self.state, self.run, self.replays, self.evaluations, self.sessions)

    def test_every_session_passing_every_evaluator_is_green(self):
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["passed"], scored["failed"], scored["unscored"]), ("green", 2, 0, 0))
        self.assertEqual((scored["recorded_cost_usd"], scored["cost_incomplete"]), ("0.20", False))
        self.assertEqual([row["case"] for row in scored["rows"]], ["Case 1", "Case 2"])

    def test_one_failed_verdict_makes_the_run_red(self):
        self.evaluations[3] = self.verdict(2, "tone", False)
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["passed"], scored["failed"]), ("red", 1, 1))
        self.assertEqual(scored["rows"][1]["evaluations"]["tone@2"]["passed"], False)

    def test_a_missing_verdict_makes_the_run_incomplete(self):
        del self.evaluations[3]
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["unscored"]), ("incomplete", 1))

    def test_a_verdict_from_other_evaluator_params_does_not_count(self):
        self.evaluations[3] = self.verdict(2, "tone", True) | {"evaluator_params": {"strict": False}}
        self.assertEqual(self.score()["outcome"], "incomplete")

    def test_a_verdict_that_is_not_true_or_false_does_not_count(self):
        self.evaluations[0] = self.verdict(1, "policy", None)
        self.assertEqual(self.score()["outcome"], "incomplete")

    def test_a_run_that_did_not_complete_is_incomplete(self):
        self.run = {"id": "run-1", "status": "failed", "error": "boom"}
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["run_error"]), ("incomplete", "boom"))

    def test_a_cohort_session_with_no_replay_is_reported_missing(self):
        del self.replays[1]
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["missing_session_ids"], scored["unscored"]), ("incomplete", ["recorded-2"], 1))

    def test_two_replays_of_one_session_do_not_cover_the_cohort(self):
        self.replays[1]["baseline_session_id"] = "recorded-1"
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["missing_session_ids"]), ("incomplete", ["recorded-2"]))

    def test_a_replay_or_session_that_errored_is_incomplete(self):
        self.sessions["result-1"] = {"status": "completed", "cost": "0.10", "error": "crashed"}
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["rows"][0]["passed"]), ("incomplete", None))
        self.sessions["result-1"] = {"status": "completed", "cost": "0.10"}
        self.replays[0]["status"] = "failed"
        self.assertEqual(self.score()["outcome"], "incomplete")

    def test_a_missing_cost_is_flagged_and_left_out_of_the_recorded_total(self):
        self.sessions["result-2"]["cost"] = None
        scored = self.score()
        self.assertEqual((scored["outcome"], scored["recorded_cost_usd"], scored["cost_incomplete"]), ("green", "0.10", True))


class LoopFixture(unittest.TestCase):
    """A Loop in Experiment 1: a red Control Experiment, then a green Run and a Run that is not submitted yet."""

    def setUp(self):
        self.cfg = loop_rules.validate_config(form(allowed_changes=["system_prompt", "model"], allowed_models=["model-b"]))
        self.state = new_state() | {
            "requested_by": "account-1",
            "preflight": {"control_estimate_usd": "0.10", "agent_version_id": "agent-version", "evaluators": ["policy@1"]},
        }
        self.state["experiments"] = [
            {"number": 0, "override": {}, "change": "Unchanged control", "experiment_id": "exp-0", "runs": [
                {"number": 1, "run_id": "run-1", "estimated_cost_usd": "0.10", "result": result("red") | {"passed": 1, "failed": 1, "unscored": 0}},
            ]},
            {"number": 1, "override": {"model": "model-b"}, "change": "Bigger model", "experiment_id": "exp-1", "runs": [
                {"number": 1, "run_id": "run-2", "estimated_cost_usd": "0.10", "result": result("green") | {"passed": 2, "failed": 0, "unscored": 0}},
                {"number": 2, "run_id": None, "estimated_cost_usd": "0.10", "result": None},
            ]},
        ]

    def drop_the_pending_run(self):
        self.state["experiments"][1]["runs"].pop()


class DescriptionTest(LoopFixture):
    def line(self, text, prefix):
        (line,) = [line for line in text.split("\n") if line.startswith(prefix)]
        return json.loads(line[len(prefix):])

    def test_the_baseline_description_carries_the_loop_settings(self):
        text = loop_rules.description(self.cfg, self.state, self.state["experiments"][0])
        first, second = text.split("\n")
        self.assertEqual(first, "Round 0: Unchanged control")
        self.assertTrue(second.startswith("Evalve loop: {"))
        settings = self.line(text, "Evalve loop: ")
        self.assertEqual((settings["schema_version"], settings["loop_id"], settings["requested_by"], settings["lap"]),
                         (1, "loop-1", "account-1", 0))
        self.assertEqual(settings["name"], "Test loop")
        self.assertEqual((settings["agent_version_id"], settings["evaluators"]), ("agent-version", ["policy@1"]))
        self.assertEqual((settings["required_consecutive_greens"], settings["max_attempts"], settings["budget_usd"]), (2, 10, "1.00"))
        self.assertEqual((settings["allowed_changes"], settings["allowed_models"]), (["system_prompt", "model"], ["model-b"]))

    def test_a_later_experiment_names_only_the_loop_and_the_lap(self):
        text = loop_rules.description(self.cfg, self.state, self.state["experiments"][1])
        self.assertEqual(text.split("\n")[0], "Round 1: Bigger model")
        self.assertEqual(self.line(text, "Evalve loop: "), {"schema_version": 1, "loop_id": "loop-1", "requested_by": "account-1", "lap": 1})

    def test_the_change_and_hypothesis_stay_on_one_line(self):
        self.state["experiments"][1]["change"] = "Bigger model\nfor the hard cases"
        self.cfg["hypothesis"] = "Larger models\nfollow the policy"
        text = loop_rules.description(self.cfg, self.state, self.state["experiments"][1])
        self.assertEqual(text.split("\n")[0], "Round 1: Bigger model for the hard cases Hypothesis: Larger models follow the policy")
        self.assertEqual(len(text.split("\n")), 2)

    def test_the_settings_line_is_compact_json_with_sorted_keys(self):
        line = loop_rules.description(self.cfg, self.state, self.state["experiments"][1]).split("\n")[1]
        self.assertEqual(line, 'Evalve loop: {"lap":1,"loop_id":"loop-1","requested_by":"account-1","schema_version":1}')

    def test_experiment_summaries_count_each_experiment(self):
        control, experiment_1 = loop_rules.experiment_summaries(self.state)
        self.assertEqual(control["runs"], [{"run_id": "run-1", "outcome": "red", "passed": 1, "failed": 1, "unscored": 0}])
        self.assertEqual((control["lap"], control["experiment_id"], control["change"]), (0, "exp-0", "Unchanged control"))
        self.assertEqual((control["attempts"], control["green"], control["red"], control["incomplete"], control["green_streak"]), (1, 0, 1, 0, 0))
        # The Run that is not submitted counts as an attempt and has no entry in `runs`.
        self.assertEqual(experiment_1["runs"], [{"run_id": "run-2", "outcome": "green", "passed": 2, "failed": 0, "unscored": 0}])
        self.assertEqual((experiment_1["lap"], experiment_1["experiment_id"], experiment_1["change"]), (1, "exp-1", "Bigger model"))
        self.assertEqual((experiment_1["attempts"], experiment_1["green"], experiment_1["red"], experiment_1["green_streak"]), (2, 1, 0, 0))

    def test_a_loop_that_is_still_going_reports_running(self):
        self.assertEqual(loop_rules.decide(self.cfg, self.state)["action"], "resume_start")
        meta = loop_rules.result_metadata(self.cfg, self.state)
        self.assertEqual((meta["status"], meta["winner_lap"], meta["reason"]), ("running", None, None))
        self.assertEqual((meta["schema_version"], meta["loop_id"], meta["baseline_model"], meta["attempts"]), (1, "loop-1", CONTROL_MODEL, 3))
        self.assertEqual(meta["laps"], loop_rules.experiment_summaries(self.state))

    def test_a_win_reports_the_winning_lap(self):
        self.state["experiments"][1]["runs"] = [
            {"number": n, "run_id": f"run-c{n}", "estimated_cost_usd": "0.10", "result": result("green")} for n in (1, 2)
        ]
        meta = loop_rules.result_metadata(self.cfg, self.state)
        self.assertEqual((meta["status"], meta["winner_lap"]), ("stop_success", 1))

    def test_a_stop_reports_its_reason(self):
        self.drop_the_pending_run()
        self.state["budget_stop"] = "The next replay was estimated at $0.10 and would exceed the budget."
        meta = loop_rules.result_metadata(self.cfg, self.state)
        self.assertEqual((meta["status"], meta["winner_lap"]), ("stop_budget", None))
        self.assertIn("exceed the budget", meta["reason"])


class DashboardGoldenTest(LoopFixture):
    """The published lines must parse under the dashboard's rules. See parse_evalve_loop."""

    def published(self) -> str:
        baseline = loop_rules.description(self.cfg, self.state, self.state["experiments"][0])
        return baseline + "\n" + loop_rules.RESULT_PREFIX + loop_rules.compact(loop_rules.result_metadata(self.cfg, self.state))

    def test_the_baseline_description_parses_into_the_setup(self):
        parsed = parse_evalve_loop(loop_rules.description(self.cfg, self.state, self.state["experiments"][0]))
        self.assertEqual(parsed["setup"], {
            "name": "Test loop", "cohort_version_id": COHORT_VERSION, "agent_version_id": "agent-version",
            "evaluators": ["policy@1"], "allowed_changes": ["system_prompt", "model"], "allowed_models": ["model-b"],
            "required_greens": 2, "max_attempts": 10, "budget_usd": "1.00",
        })
        self.assertEqual((parsed["required_greens"], parsed["result"], parsed["published_runs"]), (2, None, []))

    def test_only_the_baseline_line_is_the_loop_the_dashboard_reads(self):
        self.assertIsNone(parse_evalve_loop(loop_rules.description(self.cfg, self.state, self.state["experiments"][1])))

    def test_a_loop_that_is_still_going_parses_as_running(self):
        parsed = parse_evalve_loop(self.published())
        self.assertEqual((parsed["result"], parsed["published_runs"]), ({"status": "running"}, []))

    def test_a_win_parses_with_its_winning_lap_and_published_runs(self):
        self.state["experiments"][1]["runs"] = [
            {"number": n, "run_id": f"run-c{n}", "estimated_cost_usd": "0.10", "result": result("green")} for n in (1, 2)
        ]
        parsed = parse_evalve_loop(self.published())
        self.assertEqual(parsed["result"], {"status": "stop_success", "winner_lap": 1})
        self.assertEqual(parsed["published_runs"], [
            {"experiment_id": "exp-0", "run_id": "run-1"},
            {"experiment_id": "exp-1", "run_id": "run-c1"}, {"experiment_id": "exp-1", "run_id": "run-c2"},
        ])

    def test_each_stop_parses_with_its_reason(self):
        self.drop_the_pending_run()
        self.state["budget_stop"] = "The next replay was estimated at $0.10 and would exceed the budget."
        parsed = parse_evalve_loop(self.published())
        self.assertEqual(parsed["result"], {"status": "stop_budget", "reason": self.state["budget_stop"]})

        self.state["budget_stop"] = None
        self.cfg["max_runs"] = 2
        parsed = parse_evalve_loop(self.published())
        self.assertEqual(parsed["result"], {"status": "stop_attempt_cap", "reason": "The total replay attempt cap was reached."})

    def test_a_loop_that_stopped_as_idle_publishes_a_status_the_dashboard_does_not_read_yet(self):
        # stop_idle is new in sc-327. parseEvalveLoop reads only running, stop_success, stop_attempt_cap
        # and stop_budget, so until loops.ts changes the dashboard shows a Loop that went idle with no result.
        self.state["experiments"] = self.state["experiments"][:1]
        waiting_since = IdleLimitTest.WAITING_SINCE
        meta = loop_rules.result_metadata(self.cfg, self.state, waiting_since + timedelta(hours=25), waiting_since)
        self.assertEqual((meta["status"], meta["winner_lap"]), ("stop_idle", None))
        self.assertIn("24 hours", meta["reason"])
        lines = loop_rules.description(self.cfg, self.state, self.state["experiments"][0]) + "\n" + loop_rules.RESULT_PREFIX + loop_rules.compact(meta)
        self.assertIsNone(parse_evalve_loop(lines)["result"])

    def test_a_result_from_another_loop_is_ignored(self):
        lines = self.published().split("\n")
        lines[-1] = lines[-1].replace('"loop_id":"loop-1"', '"loop_id":"other"')
        self.assertIsNone(parse_evalve_loop("\n".join(lines))["result"])

    def test_the_parser_rejects_a_description_it_should_not_read(self):
        for text in (None, "", "Round 0: x", 'Evalve loop: {"schema_version":2,"loop_id":"a","lap":0,"required_consecutive_greens":2}',
                     'Evalve loop: {"schema_version":1,"loop_id":"a","lap":0,"required_consecutive_greens":0}'):
            with self.subTest(text=text):
                self.assertIsNone(parse_evalve_loop(text))


class GreenStreakTest(unittest.TestCase):
    def streak(self, *outcomes) -> int:
        state = new_state()
        for outcome in outcomes:
            add_run(state, outcome)
        return loop_rules.green_streak(state["experiments"][0]["runs"]) if outcomes else 0

    def test_it_counts_green_runs_from_the_last_run_backward(self):
        self.assertEqual(self.streak("red", "green", "green"), 2)
        self.assertEqual(self.streak("green", "green", "green"), 3)

    def test_it_skips_incomplete_runs(self):
        self.assertEqual(self.streak("green", "incomplete", "green"), 2)
        self.assertEqual(self.streak("incomplete", "incomplete"), 0)
        self.assertEqual(self.streak("green", "incomplete"), 1)

    def test_it_stops_at_a_red_run(self):
        self.assertEqual(self.streak("green", "red", "green"), 1)
        self.assertEqual(self.streak("green", "green", "red"), 0)

    def test_a_run_with_no_result_stops_the_count(self):
        self.assertEqual(self.streak("green", None), 0)


class NeedsChangeTest(unittest.TestCase):
    def test_a_red_run_ends_a_candidate_experiment_too(self):
        cfg, state = config(), new_state()
        add_run(state, "red")
        add_experiment(state, {"model": "model-b"}, "green", "red")
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["experiment"]), ("needs_change", 2))

    def test_an_incomplete_run_is_retried_and_a_green_run_is_replayed(self):
        cfg, state = config(), new_state()
        add_experiment(state, {}, "green")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "replay")
        add_run(state, "incomplete")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "retry")

    def test_an_experiment_with_no_run_yet_gets_its_first_run(self):
        cfg, state = config(), new_state()
        add_run(state, "red")
        add_experiment(state, {"model": "model-b"})
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["experiment"], step["green_streak"], step["runs"]), ("replay", 1, 0, 1))

    def test_a_control_experiment_with_no_run_yet_is_started(self):
        cfg, state = config(), new_state()
        add_experiment(state, {})
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["experiment"]), ("start_control", 0))

    def test_the_run_cap_is_checked_before_a_change_is_asked_for(self):
        cfg, state = config(max_runs=1), new_state()
        add_run(state, "red")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "stop_attempt_cap")

    def test_the_budget_is_checked_before_a_change_is_asked_for(self):
        cfg, state = config(budget_usd="0.10"), new_state()
        add_run(state, "red")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "stop_budget")


class IncompleteLimitTest(unittest.TestCase):
    def test_two_incomplete_runs_in_a_row_stop_the_loop(self):
        cfg, state = config(), new_state()
        add_run(state, "incomplete")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "retry")
        add_run(state, "incomplete")
        step = loop_rules.decide(cfg, state)
        self.assertEqual((step["action"], step["reason"]), ("stop_incomplete", "Two Runs in a row could not be scored."))
        self.assertEqual(loop_rules.end_state(step["action"]), {"status": "failed", "outcome": "error"})

    def test_a_green_run_between_two_incomplete_runs_resets_the_count(self):
        cfg, state = config(), new_state()
        for outcome in ("incomplete", "green", "incomplete"):
            add_run(state, outcome)
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "retry")

    def test_an_incomplete_run_of_the_previous_experiment_does_not_count(self):
        cfg, state = config(), new_state()
        add_run(state, "incomplete")
        add_run(state, "red")
        add_experiment(state, {"model": "model-b"}, "incomplete")
        self.assertEqual(loop_rules.decide(cfg, state)["action"], "retry")

    def test_the_limit_comes_before_the_budget_and_the_run_cap(self):
        state = new_state()
        add_run(state, "incomplete")
        add_run(state, "incomplete")
        self.assertEqual(loop_rules.decide(config(budget_usd="0.10", max_runs=2), state)["action"], "stop_incomplete")


class IdleLimitTest(unittest.TestCase):
    WAITING_SINCE = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def decide(self, state, hours=None, cfg=None):
        now = None if hours is None else self.WAITING_SINCE + timedelta(hours=hours)
        return loop_rules.decide(cfg or config(), state, now, self.WAITING_SINCE if hours is not None else None)

    def setUp(self):
        self.state = new_state()
        add_run(self.state, "red")

    def test_more_than_24_hours_in_needs_change_stops_the_loop(self):
        step = self.decide(self.state, hours=24.5)
        self.assertEqual(step["action"], "stop_idle")
        self.assertIn("24 hours", step["reason"])
        self.assertEqual(loop_rules.end_state(step["action"]), {"status": "completed", "outcome": "stopped_idle"})

    def test_a_loop_whose_change_arrived_is_not_idle(self):
        add_experiment(self.state, {"model": "model-b"})
        step = self.decide(self.state, hours=100)
        self.assertEqual((step["action"], step["experiment"]), ("replay", 1))

    def test_24_hours_or_less_still_needs_a_change(self):
        self.assertEqual(self.decide(self.state, hours=24)["action"], "needs_change")
        self.assertEqual(self.decide(self.state, hours=1)["action"], "needs_change")

    def test_the_idle_limit_is_not_checked_without_both_times(self):
        self.assertEqual(self.decide(self.state)["action"], "needs_change")
        self.assertEqual(loop_rules.decide(config(), self.state, self.WAITING_SINCE + timedelta(days=9))["action"], "needs_change")

    def test_a_loop_that_is_not_waiting_for_a_change_never_goes_idle(self):
        state = new_state()
        add_run(state, None)
        self.assertEqual(self.decide(state, hours=100)["action"], "collect")
        state = new_state()
        add_run(state, "green")
        self.assertEqual(self.decide(state, hours=100)["action"], "replay")

    def test_a_pass_comes_before_the_idle_limit(self):
        state = new_state()
        add_run(state, "green")
        add_run(state, "green")
        self.assertEqual(self.decide(state, hours=100)["action"], "stop_success")


class EndStateTest(LoopFixture):
    def test_each_stop_has_a_status_and_an_outcome(self):
        expected = {
            "stop_attempt_cap": ("completed", "max_runs"),
            "stop_budget": ("completed", "budget_reached"),
            "stop_idle": ("completed", "stopped_idle"),
            "stop_incomplete": ("failed", "error"),
            "cancelled": ("cancelled", "stopped_by_user"),
            "error": ("failed", "error"),
        }
        for stop, (status, outcome) in expected.items():
            with self.subTest(stop):
                self.assertEqual(loop_rules.end_state(stop), {"status": status, "outcome": outcome})

    def test_a_pass_of_the_control_experiment_is_already_passing(self):
        self.assertEqual(loop_rules.end_state("stop_success", 0), {"status": "completed", "outcome": "already_passing"})

    def test_a_pass_of_any_other_experiment_is_passed(self):
        for experiment in (1, 2, 7):
            with self.subTest(experiment):
                self.assertEqual(loop_rules.end_state("stop_success", experiment), {"status": "completed", "outcome": "passed"})

    def test_a_pass_needs_the_experiment_that_passed(self):
        with self.assertRaises(ValueError):
            loop_rules.end_state("stop_success")

    def test_the_end_state_of_a_loop_that_decide_stopped(self):
        control = new_state()
        add_run(control, "green")
        add_run(control, "green")
        step = loop_rules.decide(config(), control)
        self.assertEqual(loop_rules.end_state(step["action"], step["experiment"])["outcome"], "already_passing")

        candidate = new_state()
        add_run(candidate, "red")
        add_experiment(candidate, {"model": "model-b"}, "green", "green")
        step = loop_rules.decide(config(), candidate)
        self.assertEqual((step["action"], step["experiment"]), ("stop_success", 1))
        self.assertEqual(loop_rules.end_state(step["action"], step["experiment"])["outcome"], "passed")

    def test_every_stop_that_decide_returns_has_an_end_state(self):
        reached = {}
        state = new_state()
        add_run(state, "green")
        add_run(state, "green")
        reached["stop_success"] = loop_rules.decide(config(), state)

        state = new_state()
        add_run(state, "red")
        reached["stop_idle"] = loop_rules.decide(
            config(), state, IdleLimitTest.WAITING_SINCE + timedelta(hours=25), IdleLimitTest.WAITING_SINCE)

        reached["stop_budget"] = loop_rules.decide(config(budget_usd="0.10"), state)
        reached["stop_attempt_cap"] = loop_rules.decide(config(max_runs=1), state)

        state = new_state()
        add_run(state, "incomplete")
        add_run(state, "incomplete")
        reached["stop_incomplete"] = loop_rules.decide(config(), state)

        for expected, step in reached.items():
            with self.subTest(expected):
                self.assertEqual(step["action"], expected)
                loop_rules.end_state(step["action"], step.get("experiment"))

    def test_an_action_that_is_not_a_stop_has_no_end_state(self):
        for action in ("start_control", "replay", "retry", "needs_change", "collect", "resume_start", "running"):
            with self.subTest(action):
                with self.assertRaises(ValueError):
                    loop_rules.end_state(action)


class PureModuleTest(unittest.TestCase):
    # Parsing and arithmetic only: nothing here reaches the network, a database or the file system.
    ALLOWED_IMPORTS = {"__future__", "copy", "datetime", "decimal", "json", "re", "urllib.parse", "uuid"}
    TREE = ast.parse(Path(loop_rules.__file__).read_text(encoding="utf-8"))

    def test_the_module_reads_no_clock(self):
        # `datetime` is imported for the types and for the idle limit. The caller gives the current time.
        clock = {"now", "utcnow", "today", "fromtimestamp"}
        calls = {node.attr for node in ast.walk(self.TREE) if isinstance(node, ast.Attribute)}
        self.assertEqual(calls & clock, set())

    def test_the_module_imports_nothing_that_does_io(self):
        tree = self.TREE
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertEqual(imported - self.ALLOWED_IMPORTS, set())


if __name__ == "__main__":
    unittest.main()
