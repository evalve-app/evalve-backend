"""Each tool calls one backend route with the Teammate's key and returns the backend JSON."""

import io
import json
import logging
import os
import tempfile
import unittest

import httpx

from helpers import KEY, LOOP, START, FakeBackend, answer, session, text

ACCOUNT = {"id": "account-1", "name": "alyssa"}


class ToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_sends_the_key_as_a_bearer_token_and_returns_the_backend_json(self):
        fake = FakeBackend()
        async with session(fake) as client:
            result = await client.call_tool("evalve_loop_status", {"loop_id": "loop-1"})
        self.assertFalse(result.isError)
        self.assertEqual(json.loads(text(result)), LOOP)
        (request,) = fake.requests
        self.assertEqual((request.method, request.url.path), ("GET", "/evalve/v1/loops/loop-1"))
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")

    async def test_the_result_is_the_backend_text_with_no_change(self):
        raw = '{"loop_id":  "loop-1", "spend": {"budget_usd": "5.00"}}'
        fake = FakeBackend(httpx.Response(200, text=raw))
        async with session(fake) as client:
            result = await client.call_tool("evalve_loop_status", {"loop_id": "loop-1"})
        self.assertEqual(text(result), raw)

    async def test_start_sends_the_setup_fields_as_the_api_body(self):
        fake = FakeBackend(answer(201))
        async with session(fake) as client:
            result = await client.call_tool("evalve_loop_start", START | {"hypothesis": "The prompt is too short."})
        self.assertEqual(json.loads(text(result)), LOOP)
        (request,) = fake.requests
        self.assertEqual((request.method, request.url.path), ("POST", "/evalve/v1/loops"))
        self.assertEqual(
            fake.bodies()[0], START | {"hypothesis": "The prompt is too short.", "allow_live_tools": False},
        )
        self.assertEqual(request.extensions["timeout"]["read"], 60)

    async def test_one_start_call_repeats_with_the_same_idempotency_key(self):
        fake = FakeBackend(answer(503, {"detail": "Try again."}), answer(201))
        async with session(fake) as client:
            await client.call_tool("evalve_loop_start", START)
        first, second = (request.headers["idempotency-key"] for request in fake.requests)
        self.assertTrue(first)
        self.assertEqual(first, second)

    async def test_a_second_start_call_sends_a_new_idempotency_key(self):
        fake = FakeBackend(answer(201), answer(201))
        async with session(fake) as client:
            await client.call_tool("evalve_loop_start", START)
            await client.call_tool("evalve_loop_start", START)
        first, second = (request.headers["idempotency-key"] for request in fake.requests)
        self.assertNotEqual(first, second)

    async def test_a_name_in_use_tells_the_coding_agent_to_list_the_loops(self):
        detail = "Loop name 'Refund policy' is already in use for this agent."
        fake = FakeBackend(answer(409, {"detail": detail}))
        async with session(fake) as client:
            result = await client.call_tool("evalve_loop_start", START)
        self.assertIn(detail, text(result))
        self.assertIn("evalve_loop_list", text(result))
        self.assertEqual(len(fake.requests), 1)

    async def test_list_returns_the_loops_of_the_caller_with_the_filter_and_the_cursor(self):
        page = {"items": [LOOP], "next_cursor": None}
        fake = FakeBackend(answer(200, ACCOUNT), answer(200, page), answer(200, page))
        async with session(fake) as client:
            result = await client.call_tool("evalve_loop_list", {"status": "running", "cursor": "abc"})
            await client.call_tool("evalve_loop_list", {})
        self.assertEqual(json.loads(text(result)), page)
        me, first, second = fake.requests
        self.assertEqual(me.url.path, "/evalve/v1/me")
        self.assertEqual(first.url.path, "/evalve/v1/loops")
        self.assertEqual(dict(first.url.params), {"owner_account_id": "account-1", "status": "running", "cursor": "abc"})
        self.assertEqual(dict(second.url.params), {"owner_account_id": "account-1"})

    async def test_propose_sends_one_change_and_returns_the_experiment_id(self):
        fake = FakeBackend(answer(201, {"experiment_id": "exp-2"}))
        proposal = {"experiment": 1, "change": "Name the refund limit.", "system_prompt": "You are exact."}
        async with session(fake) as client:
            result = await client.call_tool("evalve_loop_propose_experiment", {"loop_id": "loop-1", **proposal})
        self.assertEqual(json.loads(text(result)), {"experiment_id": "exp-2"})
        (request,) = fake.requests
        self.assertEqual((request.method, request.url.path), ("POST", "/evalve/v1/loops/loop-1/experiments"))
        self.assertEqual(fake.bodies()[0], proposal)
        self.assertEqual(request.extensions["timeout"]["read"], 60)

    async def test_cancel_and_run_evidence_call_their_routes(self):
        fake = FakeBackend()
        async with session(fake) as client:
            await client.call_tool("evalve_loop_cancel", {"loop_id": "loop-1"})
            await client.call_tool("evalve_loop_run_evidence", {"loop_id": "loop-1", "experiment": 0, "run": 2})
        self.assertEqual(
            [(request.method, request.url.path) for request in fake.requests],
            [("POST", "/evalve/v1/loops/loop-1/cancel"), ("GET", "/evalve/v1/loops/loop-1/experiments/0/runs/2")],
        )
        self.assertEqual(fake.requests[0].extensions["timeout"]["read"], 15)

    async def test_a_loop_id_cannot_change_the_route(self):
        fake = FakeBackend()
        async with session(fake) as client:
            await client.call_tool("evalve_loop_status", {"loop_id": "../me?x=1"})
        self.assertEqual(fake.requests[0].url.raw_path, b"/evalve/v1/loops/..%2Fme%3Fx%3D1")

    async def test_the_tools_are_the_six_loop_tools_and_the_descriptions_give_the_poll_rule(self):
        async with session(FakeBackend()) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(set(tools), {
            "evalve_loop_start", "evalve_loop_status", "evalve_loop_list",
            "evalve_loop_propose_experiment", "evalve_loop_cancel", "evalve_loop_run_evidence",
        })
        for name in ("evalve_loop_start", "evalve_loop_status"):
            for words in ("every minute", "needs_change", "24 hours", "stopped_idle", "already_passing"):
                self.assertIn(words, tools[name].description)

    async def test_the_start_schema_gives_the_tool_policies_that_a_loop_permits(self):
        async with session(FakeBackend()) as client:
            tools = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
        models = tools["evalve_loop_start"]["$defs"]
        policy = models["ReplayPolicy"]["properties"]
        self.assertEqual(policy["type"]["description"], "`static` or `history`. A Loop refuses `passthrough` and `llm`.")
        self.assertEqual(
            policy["scope"]["description"],
            "Necessary for `history`, and it must be `baseline`: a Replay reads only the tool results of the "
            "session that it replays. A Loop refuses `cohort_version` and `agent`.",
        )
        self.assertEqual(
            policy["on_miss"]["description"],
            "What a `history` lookup does when no recorded result matches: `fail` or `error_result`. "
            "With `fail`, the Replay stops and the Run is incomplete. Two incomplete Runs in a row end the Loop. "
            "With `error_result`, the agent gets a tool error and the Loop continues.",
        )
        self.assertEqual(
            models["ToolPolicy"]["properties"]["tools"]["description"],
            "The replay policy for one tool, by the name of the tool.",
        )

    async def test_start_sends_the_scope_of_a_tool_policy(self):
        fake = FakeBackend(answer(201))
        policy = {"default": {"type": "history", "scope": "baseline", "on_miss": "fail"}, "tools": {"write": {"type": "static"}}}
        async with session(fake) as client:
            await client.call_tool("evalve_loop_start", START | {"tool_policy": policy})
        self.assertEqual(json.loads(fake.requests[0].content)["tool_policy"], policy)

    async def test_the_key_is_in_no_log_and_in_no_result(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(level)))
        fake = FakeBackend(answer(201), answer(401, {"detail": "Sign in."}), answer(422, {"detail": "Bad."}), *[answer(503)] * 3)
        async with session(fake) as client:
            results = [await client.call_tool("evalve_loop_start", START)]
            for _ in range(3):
                results.append(await client.call_tool("evalve_loop_status", {"loop_id": "loop-1"}))
        self.assertIn("HTTP Request", stream.getvalue())
        self.assertNotIn(KEY, stream.getvalue())
        for result in results:
            self.assertNotIn(KEY, text(result))

    async def test_with_no_key_each_tool_returns_the_key_message_and_makes_no_call(self):
        calls = {
            "evalve_loop_start": START, "evalve_loop_status": {"loop_id": "loop-1"}, "evalve_loop_list": {},
            "evalve_loop_propose_experiment": {"loop_id": "loop-1", "experiment": 1, "change": "x", "model": "m"},
            "evalve_loop_cancel": {"loop_id": "loop-1"},
            "evalve_loop_run_evidence": {"loop_id": "loop-1", "experiment": 0, "run": 1},
        }
        for key in (None, ""):
            fake = FakeBackend()
            async with session(fake, key=key) as client:
                for name, arguments in calls.items():
                    with self.subTest(name=name, key=key):
                        self.assertIn("Set KITARU_API_KEY", text(await client.call_tool(name, arguments)))
            self.assertEqual(fake.requests, [])

    async def test_the_server_writes_no_file_in_the_project(self):
        home = os.getcwd()
        with tempfile.TemporaryDirectory() as project:
            os.chdir(project)
            self.addCleanup(os.chdir, home)
            fake = FakeBackend(answer(201), answer(503), answer())
            async with session(fake) as client:
                await client.call_tool("evalve_loop_start", START)
                await client.call_tool("evalve_loop_status", {"loop_id": "loop-1"})
            self.assertEqual(os.listdir(project), [])


if __name__ == "__main__":
    unittest.main()
