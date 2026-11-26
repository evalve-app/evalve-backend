"""The Kitaru client: the caller's key on each request, canonical bodies, and the error kinds."""

import json
import unittest

import httpx

from evalve_backend.auth import Credential
from evalve_backend.kitaru_api import (
    KitaruApi, KitaruConflict, KitaruNotFound, KitaruRejected, KitaruTransient, canonical,
)
from helpers import KITARU_URL, TOKEN

AUTHORIZATION = f"Bearer {TOKEN}"


class KitaruApiTest(unittest.IsolatedAsyncioTestCase):
    def use(self, respond):
        self.requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return respond(request)

        client = httpx.AsyncClient(base_url=KITARU_URL, transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        return KitaruApi(client, Credential.bearer(AUTHORIZATION))

    def failing(self, status, detail="Nope."):
        return self.use(lambda request: httpx.Response(status, json={"detail": detail}))

    async def test_every_request_sends_the_callers_key(self):
        api = self.use(lambda request: httpx.Response(200, json={"items": [], "next_cursor": None, "id": "x"}))
        await api.get_run("run-1")
        await api.list_replays("run-1")
        await api.update_description("exp-1", "text")
        self.assertEqual([r.headers["authorization"] for r in self.requests], [AUTHORIZATION] * 3)

    async def test_a_body_is_canonical_json_with_its_idempotency_key(self):
        api = self.use(lambda request: httpx.Response(201, json={"id": "run-1"}))
        body = {"cohort_version_id": "c", "agent_version_id": "a", "evaluate_baselines": False, "note": "é"}
        await api.start_run("exp-1", body, "evalve-loop-exp-0-run-1")
        await api.start_run("exp-1", dict(reversed(body.items())), "evalve-loop-exp-0-run-1")
        first, second = self.requests
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.content, '{"agent_version_id":"a","cohort_version_id":"c","evaluate_baselines":false,"note":"é"}'.encode())
        self.assertEqual(first.headers["idempotency-key"], "evalve-loop-exp-0-run-1")
        self.assertEqual(first.url.path, "/api/v1/experiments/exp-1/runs")

    async def test_a_worker_is_registered_for_one_job_with_an_agent_claim_and_an_evaluator_claim(self):
        reply = {"worker": {"id": "worker-1"}, "token": "worker-token", "token_expires_at": "2026-11-25T21:00:00Z"}
        api = self.use(lambda request: httpx.Response(200, json=reply))
        self.assertEqual(await api.register_worker("evalve-loop-1", "job-1"), reply)
        (request,) = self.requests
        self.assertEqual((request.method, request.url.path), ("POST", "/api/v1/workers"))
        self.assertEqual(json.loads(request.content), {
            "name": "evalve-loop-1", "runtime": {"platform": "fargate"},
            "scope": {"claims": [{"kind": "agent"}, {"kind": "evaluator"}], "job_id": "job-1"},
        })
        self.assertNotIn("idempotency-key", request.headers)

    async def test_the_jobs_of_a_run_are_listed_page_by_page(self):
        pages = {None: {"items": [{"id": "job-1"}], "next_cursor": "next"}, "next": {"items": [{"id": "job-2"}], "next_cursor": None}}
        api = self.use(lambda request: httpx.Response(200, json=pages[request.url.params.get("cursor")]))
        self.assertEqual([job["id"] for job in await api.list_run_jobs("run-1")], ["job-1", "job-2"])
        self.assertEqual({r.url.path for r in self.requests}, {"/api/v1/experiment-runs/run-1/jobs"})

    async def test_a_worker_delete_with_no_body_succeeds_and_a_missing_worker_is_not_found(self):
        api = self.use(lambda request: httpx.Response(204))
        self.assertIsNone(await api.delete_worker("worker-1"))
        (request,) = self.requests
        self.assertEqual((request.method, request.url.path), ("DELETE", "/api/v1/workers/worker-1"))
        with self.assertRaises(KitaruNotFound):
            await self.failing(404).delete_worker("worker-1")

    def test_canonical_refuses_values_that_are_not_json(self):
        with self.assertRaises(ValueError):
            canonical({"cost": float("nan")})

    async def test_a_list_follows_the_cursor_and_sends_the_filter(self):
        pages = {None: {"items": [{"id": "r1"}], "next_cursor": "next"}, "next": {"items": [{"id": "r2"}], "next_cursor": None}}
        api = self.use(lambda request: httpx.Response(200, json=pages[request.url.params.get("cursor")]))
        self.assertEqual([r["id"] for r in await api.list_replays("run-1")], ["r1", "r2"])
        self.assertEqual(json.loads(self.requests[0].url.params["filter"]),
                         {"field": "experiment_run_id", "op": "eq", "value": "run-1"})
        self.assertEqual(self.requests[1].url.params["cursor"], "next")

    async def test_an_experiment_is_found_by_agent_and_name(self):
        api = self.use(lambda request: httpx.Response(200, json={"items": [{"id": "exp-1"}], "next_cursor": None}))
        self.assertEqual(await api.find_experiment("agent-1", "evalve-loop-exp-0"), {"id": "exp-1"})
        self.assertEqual(json.loads(self.requests[0].url.params["filter"]), {"and": [
            {"field": "agent_id", "op": "eq", "value": "agent-1"},
            {"field": "name", "op": "eq", "value": "evalve-loop-exp-0"},
        ]})

    async def test_no_match_is_none(self):
        api = self.use(lambda request: httpx.Response(200, json={"items": [], "next_cursor": None}))
        self.assertIsNone(await api.find_evaluator("policy"))

    async def test_kitaru_that_cannot_answer_now_is_transient(self):
        def unreachable(request):
            raise httpx.ConnectError("refused", request=request)

        cases = [self.use(unreachable), self.failing(500), self.failing(503), self.failing(429),
                 self.failing(401), self.failing(403), self.failing(409, "Idempotency-Key request is still in progress.")]
        for api in cases:
            with self.subTest(len(cases)), self.assertRaises(KitaruTransient):
                await api.get_run("run-1")

    async def test_a_refusal_keeps_its_status_and_detail(self):
        with self.assertRaises(KitaruNotFound) as caught:
            await self.failing(404, "Experiment run not found.").get_run("run-1")
        self.assertEqual((caught.exception.status, caught.exception.detail), (404, "Experiment run not found."))
        with self.assertRaises(KitaruConflict):
            await self.failing(409, "Experiment name 'x' is already registered").create_experiment({"name": "x"}, "key")
        with self.assertRaises(KitaruRejected) as caught:
            await self.failing(422, [{"msg": "bad"}]).get_run("run-1")
        self.assertEqual(caught.exception.detail, "bad")

    async def test_a_validation_refusal_names_the_field_and_the_rule_and_never_the_input(self):
        errors = [
            {"type": "value_error", "loc": ["body"], "msg": "Value error, a and b are mutually exclusive",
             "input": {"system_prompt": "SECRET PROMPT"}, "ctx": {"error": "SECRET CONTEXT"}},
            {"type": "missing", "loc": ["body", "cohort_version_id"], "msg": "Field required", "input": None},
            {"type": "odd"}, "not an object",
        ]
        with self.assertRaises(KitaruRejected) as caught:
            await self.failing(422, errors).start_run("01a1ef43-4c90-72a4-8ca2-e48a73b10001", {}, "key")
        refusal = caught.exception
        self.assertEqual(refusal.detail,
                         "body: Value error, a and b are mutually exclusive; body.cohort_version_id: Field required")
        self.assertEqual(refusal.request, "POST /experiments/{id}/runs")
        self.assertEqual(refusal.describe(), f"POST /experiments/{{id}}/runs, HTTP 422: {refusal.detail}")
        self.assertNotIn("SECRET", refusal.describe())

    async def test_a_refusal_with_no_usable_detail_says_so_and_stays_short(self):
        for body in ([], [{"loc": ["body"]}], {"nested": "object"}, 7):
            with self.subTest(body=body), self.assertRaises(KitaruRejected) as caught:
                await self.failing(422, body).get_run("run-1")
            self.assertEqual(caught.exception.detail, "no details")
        api = self.use(lambda request: httpx.Response(422, text="<html>"))
        with self.assertRaises(KitaruRejected) as caught:
            await api.get_run("run-1")
        self.assertEqual(caught.exception.describe(), "GET /experiment-runs/{id}, HTTP 422: no details")
        with self.assertRaises(KitaruRejected) as caught:
            await self.failing(422, [{"loc": ["body"], "msg": "x" * 500}]).get_run("run-1")
        self.assertEqual(len(caught.exception.detail), 300)

    async def test_a_dashboard_session_sends_its_cookie_and_csrf_token_on_every_call(self):
        requests: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"items": [], "next_cursor": None, "id": "x"})

        client = httpx.AsyncClient(base_url=KITARU_URL, transport=httpx.MockTransport(answer))
        self.addAsyncCleanup(client.aclose)
        api = KitaruApi(client, Credential.session("evalve_session", "session-token-value", "csrf-token-value"))
        await api.get_run("run-1")
        await api.list_replays("run-1")
        await api.start_run("exp-1", {"cohort_version_id": "c"}, "key-1")
        for request in requests:
            self.assertEqual(request.headers["cookie"], "evalve_session=session-token-value")
            self.assertEqual(request.headers["x-csrf-token"], "csrf-token-value")
            self.assertNotIn("authorization", request.headers)
        self.assertNotIn("session-token-value", repr(api) + repr(Credential.session("evalve_session", "session-token-value", "csrf")))

    async def test_a_success_that_is_not_json_is_transient(self):
        api = self.use(lambda request: httpx.Response(200, text="<html>"))
        with self.assertRaises(KitaruTransient):
            await api.get_run("run-1")

    async def test_the_key_and_bodies_are_never_logged(self):
        def unreachable(request):
            raise httpx.ConnectError(f"refused {request.headers['authorization']}", request=request)

        with self.assertLogs("evalve_backend.kitaru_api", "DEBUG") as logs:
            for api in (self.use(unreachable), self.failing(500, "secret detail"), self.failing(422, "secret detail")):
                with self.assertRaises((KitaruTransient, KitaruRejected)):
                    await api.start_run("exp-1", {"prompt": "secret body"}, "key")
        text = "\n".join(logs.output)
        for secret in (TOKEN, "secret body", "secret detail"):
            self.assertNotIn(secret, text)

    def test_the_client_hides_the_key_in_its_repr(self):
        self.assertNotIn(TOKEN, repr(KitaruApi(httpx.AsyncClient(), Credential.bearer(AUTHORIZATION))))


if __name__ == "__main__":
    unittest.main()
