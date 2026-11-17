"""The Loop rules: state goes in, a decision comes out. No network, SQL or file access.

Ported from evalve-ui/public/skills/evalve-loop/0.1.0/scripts/loop.py, with the changes of sc-324 (the
names) and sc-327 (the rules). The state is `experiments[].runs[].result`: `loop.py` calls an Experiment a
lap and a Run an attempt. The two description lines are a wire format that the dashboard reads, so they keep
the old words (`Round`, `lap`, `laps`, `attempts`, `max_attempts`, `winner_lap`).
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

SETTINGS_PREFIX = "Evalve loop: "
RESULT_PREFIX = "Evalve result: "

# A Loop that waits for a change for longer than this ends as `stop_idle`.
IDLE_LIMIT = timedelta(hours=24)

# The `status` and `outcome` that the store (sc-247) records when a Loop ends. The keys are the stops
# that `decide` returns, plus a stop by a person or the agent and a broken run or Kitaru error.
# `stop_success` is not here: its outcome depends on the Experiment (see `end_state`).
END_STATES = {
    "stop_attempt_cap": ("completed", "max_runs"),
    "stop_budget": ("completed", "budget_reached"),
    "stop_idle": ("completed", "stopped_idle"),
    "stop_incomplete": ("failed", "error"),
    "cancelled": ("cancelled", "stopped_by_user"),
    "error": ("failed", "error"),
}


class ConfigError(ValueError):
    """The Loop settings are not usable. The message says what to fix."""


def compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def dollars(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Invalid dollar amount: {value!r}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"Invalid dollar amount: {value!r}")
    return result


class CandidateError(ValueError):
    """The proposed change cannot be tried. The message says why."""


def config_dollars(cfg: dict, key: str) -> Decimal:
    try:
        return dollars(cfg[key])
    except ValueError as error:
        raise ConfigError(f"{key}: {error}") from None


def version_ref(ref: str) -> tuple[str, int]:
    if not isinstance(ref, str) or not re.fullmatch(r"\S(?:[^\r\n]*\S)?@[1-9][0-9]*", ref):
        raise ConfigError(f"Use an exact name@version reference, got {ref!r}.")
    name, version = ref.rsplit("@", 1)
    return name, int(version)


def validate_config(form: dict) -> dict:
    """Check the Loop settings and return a normalized copy. Raises ConfigError."""
    cfg = copy.deepcopy(form)
    for key in ("name", "cohort_version_id", "evaluators", "tool_policy", "allowed_changes",
                "required_consecutive_greens", "max_runs", "budget_usd"):
        if key not in cfg:
            raise ConfigError(f"Missing {key!r}.")
    cfg.setdefault("allowed_models", [])
    if cfg.get("server_url") is not None:
        server = cfg["server_url"]
        if not isinstance(server, str) or any(ch.isspace() for ch in server):
            raise ConfigError("server_url must be the Kitaru server's HTTP(S) URL.")
        try:
            target = urlsplit(server)
            valid = (
                target.scheme in ("http", "https") and target.hostname
                and target.username is None and not (target.query or target.fragment)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("server_url must be the Kitaru server's HTTP(S) URL without credentials, query, or fragment.")
        cfg["server_url"] = server.rstrip("/")
    if not isinstance(cfg["name"], str) or not re.sub(r"[^a-z0-9]+", "-", cfg["name"].lower()).strip("-"):
        raise ConfigError("Give the loop a name containing letters or numbers.")
    try:
        cfg["cohort_version_id"] = str(uuid.UUID(cfg["cohort_version_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ConfigError("cohort_version_id must be the exact cohort-version UUID.") from None
    required, cap = cfg["required_consecutive_greens"], cfg["max_runs"]
    if type(required) is not int or type(cap) is not int or not 1 <= required <= cap:
        raise ConfigError("The streak and the run cap must be positive integers, with the cap at least the streak.")
    budget = config_dollars(cfg, "budget_usd")
    if budget <= 0:
        raise ConfigError("budget_usd must be positive.")
    cfg["budget_usd"] = str(budget)

    changes = cfg["allowed_changes"]
    if not isinstance(changes, list) or not changes or any(x not in ("system_prompt", "model") for x in changes):
        raise ConfigError("allowed_changes must contain system_prompt, model, or both.")
    models = cfg["allowed_models"]
    if not isinstance(models, list) or any(not isinstance(x, str) or not x.strip() for x in models):
        raise ConfigError("allowed_models must contain model IDs.")
    if "model" in changes and not models:
        raise ConfigError("Resolve the models available to the replay worker before allowing model changes.")
    if cfg.get("agent_version") is not None:
        version_ref(cfg["agent_version"])
    if cfg.get("control_model") is not None and (
        not isinstance(cfg["control_model"], str) or not cfg["control_model"].strip()
    ):
        raise ConfigError("control_model must be the inherited model ID.")
    evaluators = cfg["evaluators"]
    if not isinstance(evaluators, list) or not evaluators:
        raise ConfigError("Select at least one binary evaluator.")
    seen = set()
    for evaluator in evaluators:
        if not isinstance(evaluator, dict):
            raise ConfigError("Each evaluator must provide a ref and optional params.")
        ref = version_ref(evaluator.get("ref"))
        if ref in seen or not isinstance(evaluator.get("params", {}), dict):
            raise ConfigError("Evaluator references must be unique and params must be objects.")
        seen.add(ref)
    policy = cfg["tool_policy"]
    if not isinstance(policy, dict) or not isinstance(policy.get("default"), dict):
        raise ConfigError("Supply the project's replay tool policy, including its default.")
    if not isinstance(policy.get("tools", {}), dict):
        raise ConfigError("tool_policy.tools must be an object.")
    policy.setdefault("tools", {})
    if any(not isinstance(p, dict) or not p.get("type") for p in [policy["default"], *policy["tools"].values()]):
        raise ConfigError("Every replay tool policy must specify a type.")
    if cfg.get("control_estimate_usd") is not None:
        cfg["control_estimate_usd"] = str(config_dollars(cfg, "control_estimate_usd"))
    return cfg


def green_streak(runs: list[dict]) -> int:
    """The green Runs of an Experiment, counted from the last Run backward.

    An incomplete Run is skipped and a red Run (or a Run with no result yet) stops the count.
    """
    count = 0
    for run in reversed(runs):
        outcome = (run["result"] or {}).get("outcome")
        if outcome == "incomplete":
            continue
        if outcome != "green":
            break
        count += 1
    return count


def accounted_spend(state: dict) -> tuple[Decimal, Decimal]:
    recorded = Decimal(0)
    charged = Decimal(0)
    for experiment in state["experiments"]:
        for run in experiment["runs"]:
            if result := run["result"]:
                cost = dollars(result["recorded_cost_usd"])
                recorded += cost
                charged += max(cost, dollars(run["estimated_cost_usd"])) if result["cost_incomplete"] else cost
    return recorded, charged


def estimate_run(state: dict, override: dict) -> Decimal | None:
    # A different model needs its own estimate. The control model's historical costs
    # can also inform prompt candidates, with the largest observed run as a floor.
    model = override.get("model", state["control_model"])
    estimate = state["preflight"]["control_estimate_usd"]
    estimates = [dollars(estimate)] if estimate is not None and model == state["control_model"] else []
    for experiment in state["experiments"]:
        if experiment["override"].get("model", state["control_model"]) == model:
            for run in experiment["runs"]:
                estimates.append(dollars(run["estimated_cost_usd"]))
                if result := run["result"]:
                    estimates.append(dollars(result["recorded_cost_usd"]))
    return max(estimates, default=None)


def decide(cfg: dict, state: dict, now: datetime | None = None, waiting_since: datetime | None = None) -> dict:
    """The next action of a Loop.

    The module reads no clock. `now` and `waiting_since` (when the last Run of the Experiment was
    collected) are given by the caller. A Loop that needs a change for more than IDLE_LIMIT stops as
    `stop_idle`. Without both times, the idle limit is not checked. Two incomplete Runs in a row stop
    the Loop as `stop_incomplete` (sc-312), so a broken setup does not use up the run cap or the budget.
    """
    experiments = state["experiments"]
    experiment = experiments[-1] if experiments else None
    runs = experiment["runs"] if experiment else []
    recorded, charged = accounted_spend(state)
    summary = {
        "runs": sum(len(e["runs"]) for e in experiments), "recorded_cost_usd": str(recorded),
        "accounted_cost_usd": str(charged), "green_streak": green_streak(runs),
    }

    def decision(action: str, **details):
        return {**summary, "action": action, **details}

    if runs and runs[-1]["result"] is None:
        run_id = runs[-1]["run_id"]
        return decision("collect" if run_id else "resume_start", experiment=experiment["number"], run_id=run_id)
    if summary["green_streak"] >= cfg["required_consecutive_greens"]:
        return decision("stop_success", experiment=experiment["number"])
    if len(runs) >= 2 and all(run["result"]["outcome"] == "incomplete" for run in runs[-2:]):
        return decision("stop_incomplete", reason="Two Runs in a row could not be scored.")
    if state.get("budget_stop"):
        return decision("stop_budget", reason=state["budget_stop"])
    if charged >= dollars(cfg["budget_usd"]):
        return decision("stop_budget", reason="The replay budget was reached.")
    if summary["runs"] >= cfg["max_runs"]:
        return decision("stop_attempt_cap", reason="The total replay attempt cap was reached.")
    if not runs:
        # An Experiment that was proposed and has no Run yet gets its first Run.
        if experiment and experiment["number"] > 0:
            return decision("replay", experiment=experiment["number"])
        return decision("start_control", experiment=0)
    outcomes = [r["result"]["outcome"] for r in runs]
    if "red" in outcomes:
        if now is not None and waiting_since is not None and now - waiting_since > IDLE_LIMIT:
            return decision("stop_idle", reason="The Loop waited more than 24 hours for a change.")
        return decision("needs_change", experiment=experiment["number"] + 1)
    return decision("retry" if outcomes[-1] == "incomplete" else "replay", experiment=experiment["number"])


def validate_candidate(cfg: dict, state: dict, override: dict, change: str, control_model: str | None) -> dict:
    """Check one proposed Experiment. Raises CandidateError.

    `override` is {"model": id} or {"system_prompt": text}; the caller reads any prompt file.
    Returns the override, the trimmed change summary and the control model to pin for the Loop.
    """
    if not isinstance(change, str) or not change.strip():
        raise CandidateError("Start a candidate with one of a model or a system prompt and a change summary.")
    if not isinstance(override, dict) or set(override) not in ({"model"}, {"system_prompt"}):
        raise CandidateError("Start a candidate with one of a model or a system prompt and a change summary.")
    pinned = state["control_model"]
    if "model" in override:
        model = override["model"]
        if "model" not in cfg["allowed_changes"] or model not in cfg["allowed_models"]:
            raise CandidateError("The candidate model must be selected in the form.")
        pinned = pinned or control_model
        if not isinstance(pinned, str) or not pinned.strip():
            raise CandidateError("Resolve the inherited model and supply the control model before trying a model candidate.")
        if control_model and control_model != pinned:
            raise CandidateError("The inherited model is already pinned for this loop.")
        if model == pinned:
            raise CandidateError("Choose a model that differs from the control.")
    else:
        if control_model is not None:
            raise CandidateError("Supply the control model only with a model candidate.")
        if "system_prompt" not in cfg["allowed_changes"]:
            raise CandidateError("The form must enable system prompt changes.")
        prompt = override["system_prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or prompt == state["control_prompt"]:
            raise CandidateError("Provide a nonempty system prompt that changes the control prompt.")
    if any(experiment["override"] == override for experiment in state["experiments"]):
        raise CandidateError("This override has already been tried. Choose a change based on the failed cases.")
    return {"override": override, "change": change.strip(), "control_model": pinned}


def score_run(state: dict, run: dict, replays: list[dict], evaluations: list[dict], sessions: dict[str, dict]) -> dict:
    """Score one finished run from what Kitaru returned for it.

    `replays` and `evaluations` are the run's rows and `sessions` maps each result session id to its
    session. A run is green only when it is complete and every cohort session passes every selected
    evaluator. The outcome is "red" when one fails and "incomplete" when the run cannot be fully scored.
    """
    result_ids = {r["result_session_id"] for r in replays if r.get("result_session_id")}
    expected = {e["version_id"]: e for e in state["evaluators"]}
    verdicts_by_session = {}
    for row in evaluations:
        session_id = row["session_id"]
        evaluator_id = row.get("evaluator_version_id")
        evaluator = expected.get(evaluator_id)
        if session_id in result_ids and evaluator and (row.get("evaluator_params") or {}) == evaluator["params"]:
            verdicts_by_session.setdefault(session_id, {})[evaluator_id] = row

    sessions = {session_id: sessions[session_id] for session_id in result_ids if session_id in sessions}
    rows = []
    for replay in replays:
        baseline_id = replay["baseline_session_id"]
        result_id = replay.get("result_session_id")
        session = sessions.get(result_id, {})
        scored = verdicts_by_session.get(result_id, {})
        verdicts = {
            f"{e['evaluator']}@{e['version']}": {
                "id": scored.get(eid, {}).get("id"), "passed": scored.get(eid, {}).get("passed"),
                "explanation": scored.get(eid, {}).get("explanation"),
            }
            for eid, e in expected.items()
        }
        complete = (
            replay["status"] == "completed" and session.get("status") == "completed" and not session.get("error")
            and all(type(verdict["passed"]) is bool for verdict in verdicts.values())
        )
        rows.append({
            "case": state["members"].get(baseline_id, baseline_id), "baseline_session_id": baseline_id,
            "replay_id": replay["id"], "replay_status": replay["status"], "result_session_id": result_id,
            "result_status": session.get("status"), "error": replay.get("error") or session.get("error"),
            "passed": all(v["passed"] for v in verdicts.values()) if complete else None,
            "evaluations": verdicts, "reply": session.get("outputs"), "cost_usd": session.get("cost"),
        })
    seen = {row["baseline_session_id"] for row in rows}
    expected_members = state["members"].keys()
    valid = (
        run["status"] == "completed" and seen == expected_members and len(rows) == len(expected_members)
        and len(result_ids) == len(expected_members) and all(row["passed"] is not None for row in rows)
    )
    failed = sum(row["passed"] is False for row in rows)
    recorded = sum((dollars(s["cost"]) for s in sessions.values() if s.get("cost") is not None), Decimal(0))
    return {
        "run_id": run["id"], "run_status": run["status"], "run_error": run.get("error"),
        "outcome": ("red" if failed else "green") if valid else "incomplete",
        "passed": sum(row["passed"] is True for row in rows), "failed": failed,
        "unscored": len(expected_members - seen) + sum(row["passed"] is None for row in rows),
        "missing_session_ids": sorted(expected_members - seen),
        "recorded_cost_usd": str(recorded),
        "cost_incomplete": len(sessions) != len(expected_members) or any(s.get("cost") is None for s in sessions.values()),
        "rows": rows,
    }


def plan_submission(cfg: dict, state: dict, step: dict, estimate: Decimal) -> dict:
    """Return {"estimate": ...} when the next replay fits the budget, else {"budget_stop": reason}."""
    if dollars(step["accounted_cost_usd"]) + estimate > dollars(cfg["budget_usd"]):
        return {"budget_stop": f"The next replay was estimated at ${estimate} and would exceed the budget."}
    return {"estimate": estimate}


def description(cfg: dict, state: dict, experiment: dict) -> str:
    """The Kitaru experiment description for one Experiment. The dashboard reads the `Evalve loop:` line."""
    number = experiment["number"]
    change = " ".join(experiment["change"].splitlines())
    if cfg.get("hypothesis"):
        change += " Hypothesis: " + " ".join(cfg["hypothesis"].splitlines())
    lines = [f"Round {number}: {change}"]
    settings = {"schema_version": 1, "loop_id": state["loop_id"], "requested_by": state["requested_by"], "lap": number}
    if number == 0:
        settings.update({key: cfg[key] for key in (
            "name", "cohort_version_id", "required_consecutive_greens",
            "budget_usd", "allowed_changes", "allowed_models",
        )})
        settings["max_attempts"] = cfg["max_runs"]
        settings["agent_version_id"] = state["preflight"]["agent_version_id"]
        settings["evaluators"] = state["preflight"]["evaluators"]
    lines.append(SETTINGS_PREFIX + compact(settings))
    return "\n".join(lines)


def experiment_summaries(state: dict) -> list[dict]:
    """One entry per Experiment, for the `laps` list of the `Evalve result:` line."""
    result = []
    for experiment in state["experiments"]:
        runs = experiment["runs"]
        outcomes = [(r["result"] or {}).get("outcome") for r in runs]
        published = []
        for run in runs:
            if run["run_id"]:
                scored = run["result"] or {}
                published.append({"run_id": run["run_id"], **{
                    key: scored.get(key) for key in ("outcome", "passed", "failed", "unscored")
                }})
        result.append({
            "lap": experiment["number"], "experiment_id": experiment["experiment_id"], "change": experiment["change"],
            "runs": published, "attempts": len(runs),
            "green": outcomes.count("green"), "red": outcomes.count("red"), "incomplete": outcomes.count("incomplete"),
            "green_streak": green_streak(runs),
        })
    return result


def result_metadata(cfg: dict, state: dict, now: datetime | None = None, waiting_since: datetime | None = None) -> dict:
    """The body of the `Evalve result:` line. Its words are the ones the dashboard reads."""
    step = decide(cfg, state, now, waiting_since)
    return {
        "schema_version": 1, "loop_id": state["loop_id"], "status": step["action"] if step["action"].startswith("stop_") else "running",
        "winner_lap": step.get("experiment") if step["action"] == "stop_success" else None,
        "baseline_model": state["control_model"],
        "attempts": step["runs"], "recorded_cost_usd": step["recorded_cost_usd"],
        "accounted_cost_usd": step["accounted_cost_usd"], "reason": step.get("reason"), "laps": experiment_summaries(state),
    }


def end_state(stop: str, experiment: int | None = None) -> dict:
    """The `status` and `outcome` for the end of a Loop.

    `stop` is a key of END_STATES or `stop_success`. A pass needs the number of the Experiment that
    passed: a pass of the Control Experiment (0) is `already_passing`, any other pass is `passed`.
    """
    if stop == "stop_success":
        if experiment is None:
            raise ValueError("A pass needs the number of the Experiment that passed.")
        return {"status": "completed", "outcome": "already_passing" if experiment == 0 else "passed"}
    if stop not in END_STATES:
        raise ValueError(f"Not the end of a Loop: {stop!r}")
    status, outcome = END_STATES[stop]
    return {"status": status, "outcome": outcome}
