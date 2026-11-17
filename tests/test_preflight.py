"""Preflight against the fake Kitaru: what it stores, and each setup it refuses before anything is spent."""

import unittest
from decimal import Decimal

import httpx

from evalve_backend import loop_rules
from evalve_backend.auth import Account
from evalve_backend.kitaru_api import KitaruApi, KitaruTransient
from evalve_backend.loop_steps import PreflightRejected
from evalve_backend.preflight import preflight, run_spec_hash
from fake_kitaru import (
    AGENT_ID, AGENT_VERSION_ID, COHORT_VERSION_ID, CONTROL_MODEL, EVALUATOR_ID, EVALUATOR_VERSION_ID, FakeKitaru,
)
from helpers import KITARU_URL, TOKEN

AUTHORIZATION = f"Bearer {TOKEN}"
ACCOUNT = Account(id="account-1", name="jack")


def config(**changes) -> dict:
    form = {
        "name": "Support loop", "cohort_version_id": COHORT_VERSION_ID,
        "evaluators": [{"ref": "policy@1", "params": {"strict": True}}],
        "tool_policy": {"default": {"type": "replay"}}, "allowed_changes": ["system_prompt"],
        "required_consecutive_greens": 2, "max_runs": 6, "budget_usd": "2.00",
    }
    return loop_rules.validate_config(form | changes)


class PreflightTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.kitaru = FakeKitaru(AUTHORIZATION)

    async def run_preflight(self, **changes):
        async with httpx.AsyncClient(base_url=KITARU_URL, transport=self.kitaru.transport()) as client:
            return await preflight(KitaruApi(client, AUTHORIZATION), config(**changes), ACCOUNT, KITARU_URL)

    async def refused(self, message: str, **changes):
        with self.assertRaises(PreflightRejected) as caught:
            await self.run_preflight(**changes)
        self.assertIn(message, str(caught.exception))

    async def test_a_runnable_setup_is_stored_with_the_first_run(self):
        prepared = await self.run_preflight()
        checked = prepared.preflight
        self.assertEqual(prepared.agent_id, AGENT_ID)
        self.assertEqual((checked["agent_version"], checked["agent_version_id"]), (f"{AGENT_ID}@2", AGENT_VERSION_ID))
        self.assertEqual(checked["run_spec_hash"], run_spec_hash(self.kitaru.agent_versions[AGENT_VERSION_ID]))
        self.assertEqual(checked["members"], {"recorded-1": "Case 1", "recorded-2": "Case 2"})
        self.assertEqual(checked["evaluators"], ["policy@1"])
        self.assertEqual(checked["evaluator_specs"], [{
            "evaluator": "policy", "version": 1, "version_id": EVALUATOR_VERSION_ID,
            "params": {"strict": True}, "connection_id": None,
        }])
        self.assertEqual((checked["control_model"], checked["control_estimate_usd"]), (CONTROL_MODEL, "0.10"))
        self.assertEqual(checked["requested_by"], {"id": "account-1", "name": "jack"})
        plan = prepared.first_run
        self.assertEqual((plan.estimated_cost_usd, plan.cohort_version_id, plan.agent_version_id),
                         (Decimal("0.10"), COHORT_VERSION_ID, AGENT_VERSION_ID))

    async def test_every_read_uses_the_callers_key(self):
        await self.run_preflight()
        self.assertTrue(self.kitaru.requests)
        self.assertEqual({r.headers["authorization"] for r in self.kitaru.requests}, {AUTHORIZATION})

    async def test_the_larger_of_the_recorded_and_the_supplied_estimate_is_used(self):
        self.assertEqual((await self.run_preflight(control_estimate_usd="0.30")).first_run.estimated_cost_usd, Decimal("0.30"))
        self.assertEqual((await self.run_preflight(control_estimate_usd="0.02")).first_run.estimated_cost_usd, Decimal("0.10"))

    async def test_a_supplied_estimate_covers_a_cohort_with_no_recorded_cost(self):
        self.kitaru.sessions["recorded-1"]["cost"] = None
        await self.refused("Provide control_estimate_usd")
        self.assertEqual((await self.run_preflight(control_estimate_usd="0.25")).first_run.estimated_cost_usd, Decimal("0.25"))

    async def test_the_supplied_control_model_wins(self):
        self.assertEqual((await self.run_preflight(control_model="gpt-large")).preflight["control_model"], "gpt-large")

    async def test_another_kitaru_server_is_refused_before_any_request(self):
        await self.refused("different Kitaru server", server_url="https://other.example.test")
        self.assertEqual(self.kitaru.requests, [])
        await self.run_preflight(server_url=KITARU_URL)

    async def test_a_missing_cohort_version_is_refused(self):
        del self.kitaru.cohort_versions[COHORT_VERSION_ID]
        await self.refused("cohort version was not found")

    async def test_an_incomplete_cohort_is_refused(self):
        self.kitaru.cohort_versions[COHORT_VERSION_ID]["session_count"] = 3
        await self.refused("complete nonempty cohort")

    async def test_mixed_recorded_versions_need_an_agent_version(self):
        self.kitaru.sessions["recorded-1"]["agent_version_id"] = "agent-version-1"
        await self.refused("Set agent_version")
        for ref in ("support-bot@2", f"{AGENT_ID}@2"):
            with self.subTest(ref):
                prepared = await self.run_preflight(agent_version=ref, control_estimate_usd="0.10")
                self.assertEqual(prepared.preflight["agent_version_id"], AGENT_VERSION_ID)

    async def test_an_agent_version_of_another_agent_or_number_is_refused(self):
        await self.refused("belongs to the cohort's agent", agent_version="other-bot@2")
        await self.refused("support-bot@9 was not found", agent_version="support-bot@9")

    async def test_a_version_without_a_run_spec_is_refused(self):
        self.kitaru.agent_versions[AGENT_VERSION_ID]["run_spec"] = None
        await self.refused("runnable agent_version")

    async def test_a_version_that_cannot_apply_overrides_or_tool_policies_is_refused(self):
        capabilities = self.kitaru.agent_versions[AGENT_VERSION_ID]["run_spec"]["runtime_capabilities"]
        capabilities["overrides"] = False
        await self.refused("cannot apply prompt or model overrides")
        capabilities["overrides"], capabilities["tool_policies"] = True, False
        await self.refused("cannot apply the configured tool policy")

    async def test_a_missing_evaluator_or_version_is_refused(self):
        await self.refused("Evaluator version policy@4 was not found", evaluators=[{"ref": "policy@4"}])
        del self.kitaru.evaluators[EVALUATOR_ID]
        await self.refused("Evaluator policy was not found")

    async def test_an_evaluator_of_another_agent_is_refused(self):
        self.kitaru.evaluators[EVALUATOR_ID]["agent_id"] = "agent-2"
        await self.refused("belongs to a different agent")

    async def test_kitaru_that_cannot_answer_is_transient(self):
        self.kitaru.fail("GET", r"/api/v1/sessions", 503)
        with self.assertRaises(KitaruTransient):
            await self.run_preflight()

    async def test_a_refused_read_explains_itself(self):
        self.kitaru.fail("GET", r"/api/v1/sessions", 422, "Invalid filter field.")
        await self.refused("Invalid filter field.")


if __name__ == "__main__":
    unittest.main()
