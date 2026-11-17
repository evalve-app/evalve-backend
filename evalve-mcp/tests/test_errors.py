"""The repeat rule, and the messages that the coding agent gets when the backend fails."""

import asyncio
import json
import unittest
from unittest import mock

import httpx

from evalve_mcp.backend import Backend
from helpers import KEY, LOOP, SERVER_URL, START, FakeBackend, answer, refused, session, slow, text

STATUS = ("evalve_loop_status", {"loop_id": "loop-1"})
EACH_TOOL = {
    "evalve_loop_start": START, "evalve_loop_status": {"loop_id": "loop-1"}, "evalve_loop_list": {},
    "evalve_loop_propose_experiment": {"loop_id": "loop-1", "experiment": 1, "change": "x", "model": "m"},
    "evalve_loop_cancel": {"loop_id": "loop-1"},
    "evalve_loop_run_evidence": {"loop_id": "loop-1", "experiment": 0, "run": 1},
}


class RepeatTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_failure_repeats_two_times_then_gives_the_plain_message(self):
        failures = {"503": answer(503, {"detail": "Kitaru could not finish the Loop request. Try again."}),
                    "connection error": refused, "time limit": slow}
        for kind, failure in failures.items():
            for name, arguments in EACH_TOOL.items():
                with self.subTest(kind=kind, tool=name):
                    fake = FakeBackend(*[failure] * 4)
                    async with session(fake) as client:
                        message = text(await client.call_tool(name, arguments))
                    self.assertEqual(len(fake.requests), 3)
                    self.assertEqual(fake.sleeps, [2, 5])
                    self.assertIn("The Loop is not lost. Call evalve_loop_status again in a minute.", message)

    async def test_a_connection_error_message_names_the_setup_command(self):
        for failure in (refused, slow):
            fake = FakeBackend(*[failure] * 3)
            async with session(fake) as client:
                message = text(await client.call_tool(*STATUS))
            self.assertIn("evalve-mcp setup --server <URL>", message)

    async def test_a_503_message_does_not_name_the_setup_command(self):
        fake = FakeBackend(*[answer(503)] * 3)
        async with session(fake) as client:
            message = text(await client.call_tool(*STATUS))
        self.assertNotIn("evalve-mcp setup", message)

    async def test_a_repeat_that_succeeds_returns_the_backend_json(self):
        for script in ([answer(503), answer()], [refused, slow, answer()]):
            fake = FakeBackend(*script)
            async with session(fake) as client:
                result = await client.call_tool(*STATUS)
            self.assertEqual(json.loads(text(result)), LOOP)
            self.assertEqual(fake.sleeps, [2, 5][: len(script) - 1])

    async def test_a_401_does_not_repeat_and_names_the_key_variable(self):
        fake = FakeBackend(answer(401, {"detail": "Sign in with a Kitaru credential."}))
        async with session(fake) as client:
            message = text(await client.call_tool(*STATUS))
        self.assertEqual((len(fake.requests), fake.sleeps), (1, []))
        self.assertIn("KITARU_API_KEY", message)

    async def test_each_other_4xx_does_not_repeat_and_passes_the_backend_message(self):
        details = {404: "Loop not found.", 409: "The Loop is not waiting for this Experiment.",
                   422: "Provide an estimate for this proposed Experiment."}
        for status, detail in details.items():
            with self.subTest(status=status):
                fake = FakeBackend(answer(status, {"detail": detail}))
                async with session(fake) as client:
                    message = text(await client.call_tool(
                        "evalve_loop_propose_experiment", EACH_TOOL["evalve_loop_propose_experiment"],
                    ))
                self.assertEqual((len(fake.requests), fake.sleeps), (1, []))
                self.assertEqual(message, detail)


class TotalTimeLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_answer_that_comes_in_slow_parts_counts_as_a_time_limit(self):
        fake = FakeBackend()
        requests = []

        async def handle(request):
            requests.append(request)
            await asyncio.sleep(3600)

        backend = Backend(SERVER_URL, KEY, version="0.1.1", transport=httpx.MockTransport(handle), sleep=fake.sleep)
        self.addAsyncCleanup(backend.close)
        with mock.patch("evalve_mcp.backend.FAST_SECONDS", 0.01):
            message = await backend.loop_status("loop-1")
        self.assertEqual((len(requests), fake.sleeps), (3, [2, 5]))
        self.assertIn("The Loop is not lost.", message)


class AccountTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_list_gives_a_plain_message_when_the_account_answer_has_no_id(self):
        for me in (httpx.Response(200, text="<html>proxy</html>"), answer(200, {"name": "alyssa"})):
            fake = FakeBackend(me)
            async with session(fake) as client:
                result = await client.call_tool("evalve_loop_list", {})
            self.assertFalse(result.isError)
            self.assertIn("gave no account ID", text(result))
            self.assertEqual(len(fake.requests), 1)


class VersionTest(unittest.IsolatedAsyncioTestCase):
    async def result(self, health, version="0.1.1", script=()):
        fake = FakeBackend(*script, health=health)
        async with session(fake, version=version) as client:
            message = text(await client.call_tool(*STATUS))
        self.assertEqual(len(fake.health_requests), 1)
        self.assertNotIn("authorization", fake.health_requests[0].headers)
        return message

    async def test_a_different_minor_or_major_number_starts_each_result_with_the_warning_line(self):
        for theirs in ("0.2.0", "1.1.1"):
            with self.subTest(theirs=theirs):
                message = await self.result(answer(200, {"status": "ok", "version": theirs}))
                warning, _, rest = message.partition("\n")
                self.assertIn("0.1.1", warning)
                self.assertIn(theirs, warning)
                self.assertIn(
                    f'uv add "evalve-mcp @ git+https://github.com/evalve-app/evalve-backend@v{theirs}#subdirectory=evalve-mcp"',
                    warning,
                )
                self.assertEqual(json.loads(rest), LOOP)

    async def test_the_warning_line_starts_an_error_message_too(self):
        health = answer(200, {"version": "0.2.0"})
        message = await self.result(health, script=[answer(404, {"detail": "Loop not found."})])
        warning, _, rest = message.partition("\n")
        self.assertIn("0.2.0", warning)
        self.assertEqual(rest, "Loop not found.")

    async def test_a_503_health_answer_with_a_version_gives_the_warning(self):
        message = await self.result(answer(503, {"status": "unavailable", "database": "unavailable", "version": "0.2.0"}))
        self.assertIn("0.2.0", message.partition("\n")[0])

    async def test_a_different_patch_number_gives_no_warning(self):
        message = await self.result(answer(200, {"version": "0.1.9"}))
        self.assertEqual(json.loads(message), LOOP)

    async def test_a_health_call_with_no_usable_answer_gives_no_warning(self):
        for health in (None, slow, httpx.Response(502, text="Bad Gateway"), answer(200, {"status": "ok"}),
                       answer(200, {"version": None}), httpx.Response(200, json=["0.2.0"])):
            with self.subTest(health=health):
                self.assertEqual(json.loads(await self.result(health)), LOOP)


if __name__ == "__main__":
    unittest.main()
