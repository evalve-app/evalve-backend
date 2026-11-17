"""The Loop rules: state goes in, a decision comes out. No network, SQL or file access.

Ported from evalve-ui/public/skills/evalve-loop/0.1.0/scripts/loop.py. The state keeps its
`laps[].attempts[].result` shape.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def dollars(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Invalid dollar amount: {value!r}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"Invalid dollar amount: {value!r}")
    return result


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


def plan_submission(cfg: dict, state: dict, step: dict, estimate: Decimal) -> dict:
    """Return {"estimate": ...} when the next replay fits the budget, else {"budget_stop": reason}."""
    if dollars(step["accounted_cost_usd"]) + estimate > dollars(cfg["budget_usd"]):
        return {"budget_stop": f"The next replay was estimated at ${estimate} and would exceed the budget."}
    return {"estimate": estimate}
