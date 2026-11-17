"""The checks before a Loop starts: the HTTP version of `preflight` in the evalve-loop `loop.py`.

Preflight reads the cohort, the agent version, the evaluators and the costs from Kitaru, with the
caller's key. It refuses a setup that cannot run, before anything is spent. What it finds is stored
with the Loop, so later steps read it from the store and do not ask Kitaru again.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from evalve_backend import loop_rules
from evalve_backend.auth import Account
from evalve_backend.kitaru_api import KitaruApi, KitaruNotFound, KitaruRejected
from evalve_backend.loop_steps import PreflightRejected, PreparedStart
from evalve_backend.store import RunPlan


def run_spec_hash(version: dict) -> str:
    """The fingerprint of what a Run executes. A change between Runs makes the Loop fail."""
    spec = {"run_spec": version.get("run_spec"), "capabilities": version.get("capabilities")}
    return hashlib.sha256(loop_rules.compact(spec).encode()).hexdigest()


async def _read(call, missing: str):
    try:
        return await call
    except KitaruNotFound:
        raise PreflightRejected(missing) from None
    except KitaruRejected as error:
        raise PreflightRejected(f"Kitaru refused a preflight read: {error.detail}") from None


async def _agent_version(api: KitaruApi, cfg: dict, agent_id: str, members: list[dict]) -> dict:
    versions = await _read(api.list_agent_versions(agent_id), "The cohort's agent was not found.")
    if cfg.get("agent_version"):
        name, number = loop_rules.version_ref(cfg["agent_version"])
        agent = await _read(api.get_agent(agent_id), "The cohort's agent was not found.")
        if name not in (agent_id, agent["name"]):
            raise PreflightRejected("Choose a runnable agent_version that belongs to the cohort's agent.")
        version = next((v for v in versions if v["version"] == number), None)
        if version is None:
            raise PreflightRejected(f"Agent version {cfg['agent_version']} was not found.")
    else:
        recorded = {session.get("agent_version_id") for session in members}
        if len(recorded) != 1 or None in recorded:
            raise PreflightRejected("The cohort has mixed or missing recorded versions. Set agent_version.")
        version = next((v for v in versions if v["id"] in recorded), None)
        if version is None:
            raise PreflightRejected("The cohort's recorded version is unavailable for replay. Set agent_version.")
    if version["agent_id"] != agent_id or not version.get("run_spec"):
        raise PreflightRejected("Choose a runnable agent_version that belongs to the cohort's agent.")
    return version


async def _evaluators(api: KitaruApi, cfg: dict, agent_id: str) -> list[dict]:
    specs = []
    for evaluator in cfg["evaluators"]:
        ref = evaluator["ref"]
        name, number = loop_rules.version_ref(ref)
        parent = await _read(api.find_evaluator(name), f"Evaluator {name} was not found.")
        if parent is None:
            raise PreflightRejected(f"Evaluator {name} was not found.")
        if parent.get("agent_id") not in (None, agent_id):
            raise PreflightRejected(f"Evaluator {ref} belongs to a different agent.")
        pinned = await _read(api.get_evaluator_version(parent["id"], number), f"Evaluator version {ref} was not found.")
        specs.append({
            "evaluator": parent["name"], "version": number, "version_id": pinned["id"],
            "params": evaluator.get("params", {}), "connection_id": evaluator.get("connection_id"),
        })
    if len({spec["version_id"] for spec in specs}) != len(specs):
        raise PreflightRejected("Select each evaluator version once.")
    return specs


def _estimate(cfg: dict, version: dict, members: list[dict]) -> Decimal:
    # Recorded costs count only when every session ran on the version that the Loop replays.
    estimates = []
    if all(m.get("agent_version_id") == version["id"] and m.get("cost") is not None for m in members):
        estimates.append(sum(loop_rules.dollars(m["cost"]) for m in members))
    if cfg.get("control_estimate_usd") is not None:
        estimates.append(loop_rules.dollars(cfg["control_estimate_usd"]))
    if not estimates:
        raise PreflightRejected("No cost is recorded for this cohort on this agent version. Provide control_estimate_usd.")
    return max(estimates)


async def preflight(api: KitaruApi, cfg: dict, account: Account, kitaru_url: str) -> PreparedStart:
    """Check a validated Loop config against Kitaru. Raises PreflightRejected or KitaruTransient."""
    if cfg.get("server_url") is not None and cfg["server_url"] != kitaru_url:
        raise PreflightRejected("server_url names a different Kitaru server from the one this backend uses.")
    cohort_version = await _read(api.get_cohort_version(cfg["cohort_version_id"]), "The cohort version was not found.")
    cohort = await _read(api.get_cohort(cohort_version["cohort_id"]), "The cohort was not found.")
    agent_id = cohort["agent_id"]
    members = await _read(api.list_cohort_sessions(cfg["cohort_version_id"]), "The cohort version was not found.")
    if not members or len({m["id"] for m in members}) != cohort_version["session_count"]:
        raise PreflightRejected("Could not read the complete nonempty cohort membership.")

    version = await _agent_version(api, cfg, agent_id, members)
    spec = version["run_spec"]
    capabilities = spec.get("runtime_capabilities") or {}
    if capabilities.get("overrides") is False:
        raise PreflightRejected("The selected agent version cannot apply prompt or model overrides.")
    if capabilities.get("tool_policies") is False:
        # Live tools are refused before preflight, so every policy here needs tool policy support.
        raise PreflightRejected("The selected agent version cannot apply the configured tool policy.")
    evaluators = await _evaluators(api, cfg, agent_id)
    estimate = _estimate(cfg, version, members)

    checked = {
        "server_url": kitaru_url, "agent_id": agent_id, "agent_version": f"{agent_id}@{version['version']}",
        "agent_version_id": version["id"], "run_spec_hash": run_spec_hash(version),
        "cohort_version_id": cfg["cohort_version_id"], "cohort_size": len(members),
        "members": {m["id"]: m.get("name") or m["id"] for m in members},
        "evaluators": [f"{e['evaluator']}@{e['version']}" for e in evaluators], "evaluator_specs": evaluators,
        "control_estimate_usd": str(estimate),
        "control_model": cfg.get("control_model") or (spec.get("env") or {}).get("OPENAI_MODEL"),
        "control_prompt": cfg.get("control_prompt"),
        "requested_by": {"id": account.id, "name": account.name},
    }
    plan = RunPlan(estimate, cfg["cohort_version_id"], version["id"], datetime.now(timezone.utc))
    return PreparedStart(agent_id, checked, plan)
