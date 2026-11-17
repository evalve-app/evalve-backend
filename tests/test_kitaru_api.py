"""The Kitaru client: the caller's key on each request, canonical bodies, and the error kinds."""

import json
import unittest

import httpx

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
        return KitaruApi(client, AUTHORIZATION)

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
        self.assertEqual(caught.exception.detail, "no details")

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
        self.assertNotIn(TOKEN, repr(KitaruApi(httpx.AsyncClient(), AUTHORIZATION)))


if __name__ == "__main__":
    unittest.main()
