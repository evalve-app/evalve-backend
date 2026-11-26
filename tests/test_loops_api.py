"""Exercise the HTTP contract with real sign-in and a fake Loop runner."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from evalve_backend.app import create_app
from evalve_backend.loop_steps import PreflightRejected, PreparedStart, StepUnavailable
from evalve_backend.store import RunPlan
from helpers import ACCOUNT, TOKEN, FakeKitaru, client, settings
from memory_store import MemoryStore

LOOPS = "/evalve/v1/loops"
TEAMMATE = ACCOUNT | {"id": "different-account", "name": "teammate"}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEAM_AUTH = {"Authorization": "Bearer teammate-token"}
START = {
    "name": "Refund triage",
    "cohort_version_id": "00000000-0000-4000-8000-000000000001",
    "evaluators": [{"ref": "refund_policy@3", "params": {"limit": 200}}, {"ref": "address_check@2"}],
    "tool_policy": {"default": {"type": "history", "scope": "baseline", "on_miss": "fail"}},
    "allowed_changes": ["system_prompt", "model"],
    "allowed_models": ["model-new"],
    "required_consecutive_greens": 2,
    "max_runs": 8,
    "budget_usd": "4.00",
    "agent_version": "triage@2",
    "control_model": "model-control",
    "control_prompt": "Use the policy.",
    "control_estimate_usd": "0.20",
    "hypothesis": "Clarify escalation.",
}
PROPOSAL = {"experiment": 1, "change": "Explain escalation", "system_prompt": "Mention the escalation rule."}
FAILED_CASE = {
    "case": "Refund outside the window", "baseline_session_id": "original", "replay_id": "replay",
    "replay_status": "completed", "result_session_id": "result", "result_status": "completed", "error": None,
    "passed": False, "evaluations": {"refund_policy@3": {"id": "evaluation", "passed": False, "explanation": "Escalate."}},
    "reply": {"text": "Try again."}, "cost_usd": "0.15",
}


SESSION = {"Cookie": "evalve_session=session-token-value", "X-CSRF-Token": "csrf-token-value"}


def identity(request):
    assert request.url.path == "/api/v1/accounts/me"
    if "Authorization" not in request.headers:
        session = (request.headers.get("Cookie"), request.headers.get("X-CSRF-Token"))
        return httpx.Response(200, json=ACCOUNT) if session == (SESSION["Cookie"], SESSION["X-CSRF-Token"]) else httpx.Response(401)
    authorization = request.headers["Authorization"]
    if authorization in (AUTH["Authorization"], "Bearer alternate-token"):
        return httpx.Response(200, json=ACCOUNT)
    if authorization == TEAM_AUTH["Authorization"]:
        return httpx.Response(200, json=TEAMMATE)
    return httpx.Response(401)


class FakeSteps:
    """Simulate the persisted checkpoints that sc-268 will produce."""

    def __init__(self, store):
        self.store = store
        self.calls = []
        self.lost_submit_reply = False
        self.cancel_unavailable = False
        self.preflight_error = None
        self.accepted_runs = {}

    async def preflight(self, config, account, credential):
        authorization = credential.headers.get("Authorization") or dict(credential.headers)
        self.calls.append(("preflight", account.id, authorization))
        if self.preflight_error:
            raise self.preflight_error
        preflight = {"agent_version_id": "agent-v2", "control_estimate_usd": "0.20",
                     "control_model": config.get("control_model")}
        plan = RunPlan(Decimal("0.20"), config["cohort_version_id"], "agent-v2", datetime.now(timezone.utc))
        return PreparedStart("agent-1", preflight, plan)

    async def submit_pending(self, owner, loop_id, experiment, credential):
        authorization = credential.headers.get("Authorization") or dict(credential.headers)
        self.calls.append(("submit", owner, authorization))
        experiment_id = f"kitaru-experiment-{loop_id}-{experiment}"
        self.store.record_experiment_id(owner, loop_id, experiment, experiment_id)
        run_id = self.accepted_runs.setdefault((loop_id, experiment), f"{experiment_id}-run")
        if self.lost_submit_reply:
            self.lost_submit_reply = False
            raise StepUnavailable(authorization)
        self.store.record_run_id(owner, loop_id, experiment, 1, run_id)

    async def advance(self, owner, loop_id, credential):
        authorization = credential.headers.get("Authorization") or dict(credential.headers)
        self.calls.append(("advance", owner, authorization))

    async def cancel(self, owner, loop_id, credential):
        authorization = credential.headers.get("Authorization") or dict(credential.headers)
        self.calls.append(("cancel", owner, authorization))
        if self.cancel_unavailable:
            raise StepUnavailable(authorization)
        self.store.transition(owner, loop_id, ("running",), "cancelled", "stopped_by_user")


class LoopApiTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.steps = FakeSteps(self.store)
        self.kitaru = FakeKitaru(identity)
        self.api = self.enterContext(client(self.kitaru, store=self.store, loop_steps=self.steps))

    def start(self, settings=START, *, key="start", headers=AUTH):
        return self.api.post(LOOPS, headers=headers | {"Idempotency-Key": key}, json=settings)

    def make_loop(self):
        response = self.start()
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["loop_id"]

    def collect_red(self, loop_id, *, age=timedelta(0)):
        run = self.store.get_run(ACCOUNT["id"], loop_id, 0, 1)
        result = {
            "run_id": run["run_id"], "run_status": "completed", "outcome": "red",
            "passed": 0, "failed": 1, "unscored": 0, "missing_session_ids": [],
            "recorded_cost_usd": "0.15", "cost_incomplete": False, "rows": [FAILED_CASE],
        }
        self.assertTrue(self.store.collect_run(
            ACCOUNT["id"], loop_id, 0, 1, result, datetime.now(timezone.utc) - age, needs_change=True,
        ))

    def test_a_dashboard_session_runs_a_loop_with_its_cookie_and_csrf_token(self):
        response = self.start(headers=SESSION)
        self.assertEqual(response.status_code, 201, response.text)
        loop_id = response.json()["loop_id"]
        self.assertEqual(self.api.get(f"{LOOPS}/{loop_id}", headers=SESSION).status_code, 200)
        self.assertEqual(self.api.post(f"{LOOPS}/{loop_id}/cancel", headers=SESSION).status_code, 200)
        self.assertEqual([call[0] for call in self.steps.calls], ["preflight", "submit", "advance", "cancel"])
        # Each step gets the session as it arrived, so its Kitaru calls pass the CSRF check too.
        self.assertTrue(all(call[2] == SESSION for call in self.steps.calls), self.steps.calls)
        # The same account with a key sees the same Loop: no route treats the two differently.
        self.assertEqual(self.api.get(f"{LOOPS}/{loop_id}", headers=AUTH).json()["loop_id"], loop_id)
        self.assertEqual(self.api.get(f"{LOOPS}/{loop_id}", headers={"Cookie": SESSION["Cookie"]}).status_code, 401)

    def test_a_list_item_carries_what_the_dashboard_list_shows(self):
        loop_id = self.make_loop()
        detail = f"{LOOPS}/{loop_id}"

        def row(**params):
            items = self.api.get(LOOPS, headers=TEAM_AUTH, params=params).json()["items"]
            return next((item for item in items if item["loop_id"] == loop_id), None)

        def agrees_with_status(item):
            status = self.api.get(detail, headers=TEAM_AUTH).json()
            self.assertEqual(item["spend"], status["spend"])
            self.assertEqual(item["experiments"], len(status["experiments"]))
            self.assertEqual(item["next_action"], (status["next"] or {}).get("action"))
            self.assertEqual(item["hypothesis"], status["setup"]["hypothesis"])
            for field in ("loop_id", "name", "slug", "agent_id", "owner_account_id", "status", "outcome", "created_at"):
                self.assertEqual(item[field], status[field])

        first = row()
        self.assertEqual((first["hypothesis"], first["experiments"], first["next_action"]), ("Clarify escalation.", 1, "collect"))
        self.assertEqual(first["spend"], {"recorded_usd": "0", "accounted_usd": "0", "budget_usd": "4"})
        agrees_with_status(first)

        # A red Control Run: the Loop waits for a change, and the spend has the cost of the Run.
        self.collect_red(loop_id)
        waiting = row()
        self.assertEqual((waiting["next_action"], waiting["spend"]["recorded_usd"]), ("needs_change", "0.15"))
        agrees_with_status(waiting)

        # A second Experiment.
        self.assertEqual(self.api.post(f"{detail}/experiments", headers=AUTH, json=PROPOSAL).status_code, 201)
        second = row()
        self.assertEqual(second["experiments"], 2)
        agrees_with_status(second)

        # The agent filter, alone and with the other filters.
        agent = second["agent_id"]
        self.assertEqual(row(agent_id=agent)["loop_id"], loop_id)
        self.assertEqual(row(agent_id=agent, status="running", owner_account_id=ACCOUNT["id"])["loop_id"], loop_id)
        self.assertIsNone(row(agent_id="another-agent"))
        self.assertIsNone(row(agent_id=agent, status="completed"))
        self.assertIsNone(row(agent_id=agent, owner_account_id=TEAMMATE["id"]))
        self.assertEqual(self.api.get(LOOPS, headers=AUTH, params={"agent_id": ""}).status_code, 422)

        # The list reads the Loop store only: no step ran, and Kitaru got identity checks only.
        calls = len(self.steps.calls)
        before = self.store.get_loop(None, loop_id)
        for _ in range(3):
            row()
        self.assertEqual(len(self.steps.calls), calls)
        self.assertEqual(self.store.get_loop(None, loop_id), before)
        self.assertEqual({request.url.path for request in self.kitaru.requests}, {"/api/v1/accounts/me"})

        # A Loop at an end state has no next action.
        self.assertEqual(self.api.post(f"{detail}/cancel", headers=AUTH).status_code, 200)
        ended = row()
        self.assertEqual((ended["status"], ended["next_action"]), ("cancelled", None))
        agrees_with_status(ended)

    def test_the_agent_filter_works_with_the_cursor(self):
        ids = [self.start(START | {"name": f"Loop {n}"}, key=f"loop-{n}").json()["loop_id"] for n in range(3)]
        agent = self.api.get(f"{LOOPS}/{ids[0]}", headers=AUTH).json()["agent_id"]
        seen, cursor = [], None
        for _ in range(4):
            params = {"agent_id": agent, "limit": 2} | ({"cursor": cursor} if cursor else {})
            page = self.api.get(LOOPS, headers=AUTH, params=params).json()
            seen += [item["loop_id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, list(reversed(ids)))

    def test_equivalent_start_reuses_existing_loop_before_preflight(self):
        loop_id = self.make_loop()
        same = dict(reversed(list(START.items()))) | {
            "allow_live_tools": False, "budget_usd": 4, "tool_policy": START["tool_policy"] | {"tools": {}},
            "evaluators": [START["evaluators"][0], {"ref": "address_check@2", "params": {}}],
        }
        reply = self.start(same, headers={"Authorization": "Bearer alternate-token"})
        self.assertEqual((reply.status_code, reply.json()["loop_id"]), (200, loop_id))
        self.assertEqual(self.start(START | {"budget_usd": "5"}).status_code, 409)
        self.assertEqual([call[0] for call in self.steps.calls], ["preflight", "submit"])
        self.assertEqual(len(self.store.list_loops(None)), 1)
        self.assertEqual(len(self.kitaru.requests), 3)

    def test_retry_recovers_pending_control_after_lost_kitaru_reply(self):
        self.steps.lost_submit_reply = True
        first = self.start()
        self.assertEqual(first.status_code, 503)
        self.assertNotIn(TOKEN, first.text)
        [saved] = self.store.list_loops(None)
        self.assertIsNone(self.store.get_run(None, saved["loop_id"], 0, 1)["run_id"])
        recovered = self.start()
        self.assertEqual((recovered.status_code, recovered.json()["loop_id"]), (200, saved["loop_id"]))
        experiment = recovered.json()["experiments"][0]
        self.assertIsNotNone(experiment["experiment_id"])
        self.assertIsNotNone(experiment["runs"][0]["run_id"])
        self.assertEqual(len(self.steps.accepted_runs), 1)
        self.assertEqual([call[0] for call in self.steps.calls], ["preflight", "submit", "submit"])
        self.assertTrue(all(call[2] == AUTH["Authorization"] for call in self.steps.calls))

    def test_teammate_reads_all_loops_and_evidence_but_cannot_advance_or_write(self):
        loop_id = self.make_loop()
        detail = f"{LOOPS}/{loop_id}"
        evidence = f"{detail}/experiments/0/runs/1"
        self.assertEqual(self.api.get(detail, headers=TEAM_AUTH).json()["next"]["action"], "collect")
        self.assertEqual([call[0] for call in self.steps.calls], ["preflight", "submit"])
        self.assertEqual(self.api.get(detail, headers=AUTH).status_code, 200)
        self.assertEqual(self.steps.calls[-1], ("advance", ACCOUNT["id"], AUTH["Authorization"]))

        self.collect_red(loop_id, age=timedelta(hours=25))
        before = self.store.get_loop(None, loop_id)
        listing = self.api.get(LOOPS, headers=TEAM_AUTH).json()
        self.assertEqual([row["loop_id"] for row in listing["items"]], [loop_id])
        status = self.api.get(detail, headers=TEAM_AUTH)
        self.assertEqual(status.json()["status"], "running")
        self.assertEqual(status.json()["next"]["action"], "needs_change")
        self.assertEqual(status.json()["needs"]["failed_cases"][0]["case"], FAILED_CASE["case"])
        self.assertEqual(self.api.get(evidence, headers=TEAM_AUTH).json()["failed_cases"], [FAILED_CASE])
        self.assertEqual(self.api.get(f"{detail}/report", headers=TEAM_AUTH).status_code, 409)
        self.assertEqual(self.api.post(f"{detail}/experiments", headers=TEAM_AUTH, json=PROPOSAL).status_code, 404)
        self.assertEqual(self.api.post(f"{detail}/cancel", headers=TEAM_AUTH).status_code, 404)
        self.assertEqual(self.store.get_loop(None, loop_id), before)
        self.assertEqual([call[0] for call in self.steps.calls], ["preflight", "submit", "advance"])
        self.assertEqual(self.api.get(detail, headers=AUTH).json()["outcome"], "stopped_idle")
        self.assertEqual(self.api.get(f"{detail}/report", headers=TEAM_AUTH).json()["outcome"], "stopped_idle")

    def test_active_name_conflict_across_accounts_returns_only_the_requested_name(self):
        loop_id = self.make_loop()
        reply = self.start(headers=TEAM_AUTH)
        self.assertEqual(reply.status_code, 409)
        self.assertIn(START["name"], reply.json()["detail"])
        self.assertNotIn(loop_id, reply.text)
        self.assertNotIn(ACCOUNT["id"], reply.text)

    def test_all_routes_require_a_fresh_identity_check(self):
        loop_id = self.make_loop()
        detail = f"{LOOPS}/{loop_id}"
        requests = [
            ("GET", LOOPS, {}), ("GET", detail, {}), ("GET", f"{detail}/report", {}),
            ("GET", f"{detail}/experiments/0/runs/1", {}),
            ("POST", LOOPS, {"json": START}), ("POST", f"{detail}/experiments", {"json": PROPOSAL}),
            ("POST", f"{detail}/cancel", {}),
        ]
        for code, status in ((200, 401), (500, 503)):
            self.kitaru.respond = lambda _request: httpx.Response(code, json=ACCOUNT | {"active": False})
            for method, path, args in requests:
                with self.subTest(method=method, path=path, status=status):
                    before = len(self.kitaru.requests)
                    response = self.api.request(method, path, headers=AUTH | {"Idempotency-Key": "test"}, **args)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(len(self.kitaru.requests), before + 1)
                    self.assertNotIn("Retry-After", response.headers)
        self.assertEqual([call[0] for call in self.steps.calls], ["preflight", "submit"])

    def test_malformed_json_still_checks_identity_once(self):
        headers = AUTH | {"Content-Type": "application/json"}
        self.assertEqual(self.api.post(LOOPS, headers=headers, content="{").status_code, 422)
        self.assertEqual(len(self.kitaru.requests), 1)
        self.kitaru.respond = lambda _request: httpx.Response(503)
        self.assertEqual(self.api.post(LOOPS, headers=headers, content="{").status_code, 503)
        self.assertEqual(len(self.kitaru.requests), 2)

    def test_setup_validation_including_live_tools_runs_before_preflight(self):
        invalid = {
            "name": "!!!", "cohort_version_id": "latest", "evaluators": [{"ref": "latest"}],
            "tool_policy": {}, "allowed_changes": [], "allowed_models": [],
            "required_consecutive_greens": True, "max_runs": 1, "budget_usd": 0,
            "agent_version": "latest", "control_model": "", "control_prompt": {},
            "control_estimate_usd": -1, "hypothesis": 12, "allow_live_tools": True,
        }
        for key, value in invalid.items():
            with self.subTest(key=key):
                response = self.start(START | {key: value})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], str)
        for policy in (
            {"default": {"type": "passthrough"}},
            {"default": {"type": "history", "on_miss": "passthrough"}},
            {"default": {"type": "history"}, "tools": {"write": {"type": "passthrough"}}},
        ):
            self.assertEqual(self.start(START | {"tool_policy": policy}).status_code, 422)
        self.assertEqual(self.api.post(LOOPS, headers=AUTH, json=START).status_code, 422)
        self.assertEqual(self.steps.calls, [])
        self.assertEqual(self.store.list_loops(None), [])

    def test_preflight_rejection_has_a_plain_message_and_does_not_create_a_loop(self):
        self.steps.preflight_error = PreflightRejected("Select a nonempty cohort.")
        response = self.start()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "Select a nonempty cohort."})
        self.assertEqual(self.store.list_loops(None), [])

    def test_proposal_validation_retry_and_successful_report(self):
        loop_id = self.make_loop()
        path = f"{LOOPS}/{loop_id}/experiments"
        self.assertEqual(self.api.post(path, headers=AUTH, json=PROPOSAL).status_code, 409)
        self.collect_red(loop_id)
        self.assertEqual(self.api.post(path, headers=AUTH, json=PROPOSAL | {"experiment": 2}).status_code, 409)
        self.assertEqual(self.api.post(path, headers=AUTH, json=PROPOSAL | {"system_prompt": START["control_prompt"]}).status_code, 422)
        no_estimate = {"experiment": 1, "change": "Try a different model", "model": "model-new"}
        self.assertEqual(self.api.post(path, headers=AUTH, json=no_estimate).status_code, 422)
        first = self.api.post(path, headers=AUTH, json=PROPOSAL)
        self.assertEqual(first.status_code, 201, first.text)
        same = self.api.post(path, headers=AUTH, json=PROPOSAL)
        self.assertEqual((same.status_code, same.json()), (201, first.json()))
        self.assertEqual(self.api.post(path, headers=AUTH, json=PROPOSAL | {"system_prompt": "Different."}).status_code, 409)
        self.assertEqual([call[0] for call in self.steps.calls].count("submit"), 2)
        self.store.transition(ACCOUNT["id"], loop_id, ("running",), "completed", "passed")
        report = self.api.get(f"{LOOPS}/{loop_id}/report", headers=TEAM_AUTH).json()
        self.assertEqual(report["outcome"], "passed")
        self.assertEqual(report["winner"]["override"], {"system_prompt": PROPOSAL["system_prompt"]})
        self.assertEqual(report["winner"]["experiment_id"], first.json()["experiment_id"])

    def test_late_proposal_expires_the_wait_without_starting_an_experiment(self):
        loop_id = self.make_loop()
        self.collect_red(loop_id, age=timedelta(hours=25))
        reply = self.api.post(f"{LOOPS}/{loop_id}/experiments", headers=AUTH, json=PROPOSAL)
        self.assertEqual(reply.status_code, 409)
        saved = self.store.get_loop(None, loop_id)
        self.assertEqual((saved["status"], saved["outcome"]), ("completed", "stopped_idle"))
        self.assertEqual(len(saved["experiments"]), 1)
        self.assertEqual([call[0] for call in self.steps.calls].count("submit"), 1)

    def test_cancel_requires_confirmation_and_repeats_without_more_kitaru_work(self):
        loop_id = self.make_loop()
        path = f"{LOOPS}/{loop_id}/cancel"
        self.steps.cancel_unavailable = True
        self.assertEqual(self.api.post(path, headers=AUTH).status_code, 503)
        self.assertEqual(self.store.get_loop(None, loop_id)["status"], "running")
        self.steps.cancel_unavailable = False
        cancelled = self.api.post(path, headers=AUTH)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["outcome"], "stopped_by_user")
        self.assertEqual(self.api.post(path, headers=AUTH).json(), cancelled.json())
        self.assertEqual([call[0] for call in self.steps.calls].count("cancel"), 2)
        self.assertIsNone(self.api.get(f"{LOOPS}/{loop_id}/report", headers=TEAM_AUTH).json()["winner"])

    def test_shared_pagination_and_status_filter(self):
        first = self.make_loop()
        second = self.start(START | {"name": "Another Loop"}, headers=TEAM_AUTH)
        self.assertEqual(second.status_code, 201)
        page = self.api.get(LOOPS, headers=AUTH, params={"limit": 1}).json()
        self.assertEqual([row["loop_id"] for row in page["items"]], [second.json()["loop_id"]])
        last = self.api.get(LOOPS, headers=AUTH, params={"limit": 1, "cursor": page["next_cursor"]}).json()
        self.assertEqual([row["loop_id"] for row in last["items"]], [first])
        self.assertIsNone(last["next_cursor"])
        self.assertEqual(self.api.get(LOOPS, headers=AUTH, params={"status": "completed"}).json()["items"], [])
        self.assertEqual(self.api.get(LOOPS, headers=AUTH, params={"cursor": "-bad-"}).status_code, 422)

    def test_start_that_kitaru_cannot_check_does_not_create_a_loop(self):
        # The default Loop steps call Kitaru. Here Kitaru answers only the sign-in check.
        def sign_in_only(request):
            if request.url.path == "/api/v1/accounts/me":
                return identity(request)
            return httpx.Response(503, json={"detail": "Unavailable."})

        with client(FakeKitaru(sign_in_only), store=self.store) as api:
            reply = api.post(LOOPS, headers=AUTH | {"Idempotency-Key": "start"}, json=START)
            self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.store.list_loops(None), [])

    def test_owner_filter_keeps_newest_first_pagination(self):
        oldest = self.make_loop()
        teammate = self.start(START | {"name": "Teammate's Loop"}, headers=TEAM_AUTH)
        newest = self.start(START | {"name": "Newest Loop"}, key="newest")
        self.assertEqual([teammate.status_code, newest.status_code], [201, 201])

        filters = {"owner_account_id": ACCOUNT["id"], "limit": 1}
        page = self.api.get(LOOPS, headers=TEAM_AUTH, params=filters).json()
        self.assertEqual([row["loop_id"] for row in page["items"]], [newest.json()["loop_id"]])
        second = self.api.get(LOOPS, headers=TEAM_AUTH, params=filters | {"cursor": page["next_cursor"]}).json()
        self.assertEqual([row["loop_id"] for row in second["items"]], [oldest])
        self.assertIsNone(second["next_cursor"])


class OpenApiTest(unittest.TestCase):
    def test_checked_in_spec_matches_the_app_and_declares_the_loop_enums(self):
        path = Path(__file__).resolve().parents[1] / "openapi" / "evalve-backend.json"
        schema = create_app(settings()).openapi()
        self.assertEqual(json.loads(path.read_text()), schema)
        models = schema["components"]["schemas"]
        self.assertEqual(set(models["LoopDetail"]["properties"]["status"]["enum"]), {"running", "completed", "failed", "cancelled"})
        self.assertIn("passed", models["LoopDetail"]["properties"]["outcome"]["anyOf"][0]["enum"])
        self.assertIn("needs_change", models["LoopNext"]["properties"]["action"]["enum"])
        item = models["LoopListItem"]["properties"]
        self.assertLessEqual({"hypothesis", "experiments", "spend", "next_action"}, set(item))
        self.assertEqual(models["LoopList"]["properties"]["items"]["items"], {"$ref": "#/components/schemas/LoopListItem"})
        parameters = {p["name"] for p in schema["paths"][LOOPS]["get"]["parameters"]}
        self.assertLessEqual({"agent_id", "status", "owner_account_id", "cursor", "limit"}, parameters)

    def test_each_signed_in_route_declares_both_ways_to_sign_in(self):
        schema = create_app(settings()).openapi()
        schemes = schema["components"]["securitySchemes"]
        self.assertEqual((schemes["SessionCookie"]["in"], schemes["SessionCookie"]["name"]), ("cookie", "evalve_session"))
        self.assertEqual((schemes["CsrfToken"]["in"], schemes["CsrfToken"]["name"]), ("header", "X-CSRF-Token"))
        both = [{"HTTPBearer": []}, {"SessionCookie": [], "CsrfToken": []}]
        signed_in = 0
        for path, operations in schema["paths"].items():
            for operation in operations.values():
                if path == "/evalve/health":
                    self.assertNotIn("security", operation)
                elif path == f"{LOOPS}/{{loop_id}}/task/poll":
                    # A Loop task shows its secret. A Kitaru credential gets 401 on this route.
                    self.assertEqual(operation["security"], [{"LoopTaskSecret": []}])
                    self.assertEqual((schemes["LoopTaskSecret"]["in"], schemes["LoopTaskSecret"]["name"]),
                                     ("header", "Authorization"))
                else:
                    self.assertEqual(operation["security"], both, path)
                    signed_in += 1
        self.assertGreater(signed_in, 5)

    def test_all_loop_operations_document_the_plain_validation_error(self):
        paths = create_app(settings()).openapi()["paths"]
        for path, operations in paths.items():
            if path != LOOPS and not path.startswith(f"{LOOPS}/"):
                continue
            for method, operation in operations.items():
                with self.subTest(path=path, method=method):
                    schema = operation["responses"]["422"]["content"]["application/json"]["schema"]
                    self.assertEqual(schema, {"$ref": "#/components/schemas/ApiError"})
