"""The Loop rules: state goes in, a decision comes out. No network, SQL or file access.

Ported from evalve-ui/public/skills/evalve-loop/0.1.0/scripts/loop.py. The state keeps its
`laps[].attempts[].result` shape.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

SETTINGS_PREFIX = "Evalve loop: "
RESULT_PREFIX = "Evalve result: "

# The `status` and `outcome` that the store (sc-247) records when a Loop ends. The keys are the stops
# that `decide` returns, plus a stop by a person or the agent and a broken run or Kitaru error.
END_STATES = {
    "stop_success": ("completed", "target_reached"),
    "stop_attempt_cap": ("completed", "max_runs"),
    "stop_budget": ("completed", "budget_reached"),
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
                "required_consecutive_greens", "max_attempts", "budget_usd"):
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
    required, cap = cfg["required_consecutive_greens"], cfg["max_attempts"]
    if type(required) is not int or type(cap) is not int or not 1 <= required <= cap:
        raise ConfigError("The streak and attempt cap must be positive integers, with the cap at least the streak.")
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
    if cfg.get("baseline_model") is not None and (
        not isinstance(cfg["baseline_model"], str) or not cfg["baseline_model"].strip()
    ):
        raise ConfigError("baseline_model must be the inherited model ID.")
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
    if cfg.get("baseline_estimate_usd") is not None:
        cfg["baseline_estimate_usd"] = str(config_dollars(cfg, "baseline_estimate_usd"))
    return cfg


def green_streak(attempts: list[dict]) -> int:
    count = 0
    for attempt in reversed(attempts):
        if (attempt["result"] or {}).get("outcome") != "green":
            break
        count += 1
    return count


def accounted_spend(state: dict) -> tuple[Decimal, Decimal]:
    recorded = Decimal(0)
    charged = Decimal(0)
    for lap in state["laps"]:
        for attempt in lap["attempts"]:
            if result := attempt["result"]:
                cost = dollars(result["recorded_cost_usd"])
                recorded += cost
                charged += max(cost, dollars(attempt["estimated_cost_usd"])) if result["cost_incomplete"] else cost
    return recorded, charged


def estimate_attempt(state: dict, override: dict) -> Decimal | None:
    # A different model needs its own estimate. The baseline model's historical costs
    # can also inform prompt candidates, with the largest observed attempt as a floor.
    model = override.get("model", state["baseline_model"])
    estimate = state["preflight"]["baseline_estimate_usd"]
    estimates = [dollars(estimate)] if estimate is not None and model == state["baseline_model"] else []
    for lap in state["laps"]:
        if lap["override"].get("model", state["baseline_model"]) == model:
            for attempt in lap["attempts"]:
                estimates.append(dollars(attempt["estimated_cost_usd"]))
                if result := attempt["result"]:
                    estimates.append(dollars(result["recorded_cost_usd"]))
    return max(estimates, default=None)


def decide(cfg: dict, state: dict) -> dict:
    laps = state["laps"]
    lap = laps[-1] if laps else None
    attempts = lap["attempts"] if lap else []
    recorded, charged = accounted_spend(state)
    summary = {
        "attempts": sum(len(l["attempts"]) for l in laps), "recorded_cost_usd": str(recorded),
        "accounted_cost_usd": str(charged), "green_streak": green_streak(attempts),
    }

    def decision(action: str, **details):
        return {**summary, "action": action, **details}

    if attempts and attempts[-1]["result"] is None:
        run_id = attempts[-1]["run_id"]
        return decision("collect" if run_id else "resume_start", lap=lap["number"], run_id=run_id)
    if summary["green_streak"] >= cfg["required_consecutive_greens"]:
        return decision("stop_success", lap=lap["number"])
    if state.get("budget_stop"):
        return decision("stop_budget", reason=state["budget_stop"])
    if charged >= dollars(cfg["budget_usd"]):
        return decision("stop_budget", reason="The replay budget was reached.")
    if summary["attempts"] >= cfg["max_attempts"]:
        return decision("stop_attempt_cap", reason="The total replay attempt cap was reached.")
    if not attempts:
        return decision("start_baseline", lap=0)
    outcomes = [a["result"]["outcome"] for a in attempts]
    baseline_complete = lap["number"] == 0 and sum(x != "incomplete" for x in outcomes) >= cfg["required_consecutive_greens"]
    if (baseline_complete and "red" in outcomes) or (lap["number"] > 0 and outcomes[-1] == "red"):
        return decision("start_candidate", lap=lap["number"] + 1)
    return decision("retry" if outcomes[-1] == "incomplete" else "replay", lap=lap["number"])


def validate_candidate(cfg: dict, state: dict, override: dict, change: str, baseline_model: str | None) -> dict:
    """Check one proposed Round. Raises CandidateError.

    `override` is {"model": id} or {"system_prompt": text}; the caller reads any prompt file.
    Returns the override, the trimmed change summary and the baseline model to pin for the Loop.
    """
    if not isinstance(change, str) or not change.strip():
        raise CandidateError("Start a candidate with one of a model or a system prompt and a change summary.")
    if set(override) not in ({"model"}, {"system_prompt"}):
        raise CandidateError("Start a candidate with one of a model or a system prompt and a change summary.")
    pinned = state["baseline_model"]
    if "model" in override:
        model = override["model"]
        if "model" not in cfg["allowed_changes"] or model not in cfg["allowed_models"]:
            raise CandidateError("The candidate model must be selected in the form.")
        pinned = pinned or baseline_model
        if not pinned or not pinned.strip():
            raise CandidateError("Resolve the inherited model and supply the baseline model before trying a model candidate.")
        if baseline_model and baseline_model != pinned:
            raise CandidateError("The inherited model is already pinned for this loop.")
        if model == pinned:
            raise CandidateError("Choose a model that differs from the baseline.")
    else:
        if baseline_model is not None:
            raise CandidateError("Supply the baseline model only with a model candidate.")
        if "system_prompt" not in cfg["allowed_changes"]:
            raise CandidateError("The form must enable system prompt changes.")
        prompt = override["system_prompt"]
        if not prompt.strip() or prompt == state["baseline_prompt"]:
            raise CandidateError("Provide a nonempty system prompt that changes the baseline.")
    if any(lap["override"] == override for lap in state["laps"]):
        raise CandidateError("This override has already been tried. Choose a change based on the failed cases.")
    return {"override": override, "change": change.strip(), "baseline_model": pinned}


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


def description(cfg: dict, state: dict, lap: dict) -> str:
    """The experiment description for one lap. The dashboard reads the `Evalve loop:` line."""
    number = lap["number"]
    change = " ".join(lap["change"].splitlines())
    if cfg.get("hypothesis"):
        change += " Hypothesis: " + " ".join(cfg["hypothesis"].splitlines())
    lines = [f"Round {number}: {change}"]
    settings = {"schema_version": 1, "loop_id": state["loop_id"], "requested_by": state["requested_by"], "lap": number}
    if number == 0:
        settings.update({key: cfg[key] for key in (
            "name", "cohort_version_id", "required_consecutive_greens", "max_attempts",
            "budget_usd", "allowed_changes", "allowed_models",
        )})
        settings["agent_version_id"] = state["preflight"]["agent_version_id"]
        settings["evaluators"] = state["preflight"]["evaluators"]
    lines.append(SETTINGS_PREFIX + compact(settings))
    return "\n".join(lines)


def lap_summaries(state: dict) -> list[dict]:
    result = []
    for lap in state["laps"]:
        attempts = lap["attempts"]
        outcomes = [(a["result"] or {}).get("outcome") for a in attempts]
        runs = []
        for attempt in attempts:
            if attempt["run_id"]:
                scored = attempt["result"] or {}
                runs.append({"run_id": attempt["run_id"], **{
                    key: scored.get(key) for key in ("outcome", "passed", "failed", "unscored")
                }})
        result.append({
            "lap": lap["number"], "experiment_id": lap["experiment_id"], "change": lap["change"],
            "runs": runs, "attempts": len(attempts),
            "green": outcomes.count("green"), "red": outcomes.count("red"), "incomplete": outcomes.count("incomplete"),
            "green_streak": green_streak(attempts),
        })
    return result


def result_metadata(cfg: dict, state: dict) -> dict:
    """The body of the `Evalve result:` line. Its words are the ones the dashboard reads."""
    step = decide(cfg, state)
    return {
        "schema_version": 1, "loop_id": state["loop_id"], "status": step["action"] if step["action"].startswith("stop_") else "running",
        "winner_lap": step.get("lap") if step["action"] == "stop_success" else None,
        "baseline_model": state["baseline_model"],
        "attempts": step["attempts"], "recorded_cost_usd": step["recorded_cost_usd"],
        "accounted_cost_usd": step["accounted_cost_usd"], "reason": step.get("reason"), "laps": lap_summaries(state),
    }


def end_state(stop: str) -> dict:
    """The `status` and `outcome` for the end of a Loop. `stop` is a key of END_STATES."""
    if stop not in END_STATES:
        raise ValueError(f"Not the end of a Loop: {stop!r}")
    status, outcome = END_STATES[stop]
    return {"status": status, "outcome": outcome}
