import logging
import unittest

import httpx

from helpers import ACCOUNT, KITARU_URL, TOKEN, FakeKitaru, client

ME = "/evalve/v1/me"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


class SignInTest(unittest.TestCase):
    def test_a_kitaru_credential_returns_the_account(self):
        kitaru = FakeKitaru()
        with client(kitaru) as api:
            response = api.get(ME, headers=BEARER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": ACCOUNT["id"], "name": "alyssa"})
        [request] = kitaru.requests
        self.assertEqual(str(request.url), f"{KITARU_URL}/api/v1/accounts/me")
        self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")

    def test_the_caller_cannot_choose_which_kitaru_is_asked(self):
        kitaru = FakeKitaru()
        elsewhere = {
            **BEARER, "Host": "attacker.example", "X-Forwarded-Host": "attacker.example",
            "X-Kitaru-Url": "https://attacker.example",
        }
        with client(kitaru) as api:
            response = api.get(f"{ME}?server=https://attacker.example", headers=elsewhere)
        self.assertEqual(response.status_code, 200)
        [request] = kitaru.requests
        self.assertEqual(str(request.url), f"{KITARU_URL}/api/v1/accounts/me")
        self.assertEqual(request.headers["Host"], "kitaru.internal:8000")

    def test_a_missing_credential_is_refused_without_asking_kitaru(self):
        kitaru = FakeKitaru()
        with client(kitaru) as api:
            response = api.get(ME)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(kitaru.requests, [])

    def test_a_credential_that_is_not_a_bearer_token_is_refused(self):
        kitaru = FakeKitaru()
        with client(kitaru) as api:
            for value in ("Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", TOKEN, "Bearer " + "x" * 5000):
                with self.subTest(value=value[:20]):
                    self.assertEqual(api.get(ME, headers={"Authorization": value}).status_code, 401)
        self.assertEqual(kitaru.requests, [])

    def test_a_credential_with_bytes_outside_ascii_is_refused(self):
        # Raw bytes, so the test client sends them as they are.
        kitaru = FakeKitaru()
        with client(kitaru) as api:
            for value in (b"Bearer caf\xe9", "Bearer café".encode(), b"Bearer key\x7f", b"Bearer \xff\xfe"):
                with self.subTest(value=value):
                    self.assertEqual(api.get(ME, headers={"Authorization": value}).status_code, 401)
        self.assertEqual(kitaru.requests, [])

    def test_a_credential_kitaru_rejects_is_refused(self):
        # Kitaru answers 401 for an invalid or expired credential.
        for status in (401, 403):
            with self.subTest(status=status):
                kitaru = FakeKitaru(lambda request, status=status: httpx.Response(status, json={"detail": "no"}))
                with client(kitaru) as api:
                    self.assertEqual(api.get(ME, headers=BEARER).status_code, 401)
                self.assertEqual(len(kitaru.requests), 1)

    def test_a_deactivated_account_is_refused(self):
        kitaru = FakeKitaru(lambda request: httpx.Response(200, json=ACCOUNT | {"active": False}))
        with client(kitaru) as api:
            self.assertEqual(api.get(ME, headers=BEARER).status_code, 401)

    def test_a_kitaru_failure_is_not_treated_as_signed_in(self):
        def unreachable(request):
            raise httpx.ConnectError("connection refused", request=request)

        answers = {
            "server error": lambda request: httpx.Response(500, text="boom"),
            "redirect": lambda request: httpx.Response(302, headers={"Location": "https://attacker.example/"}),
            "not json": lambda request: httpx.Response(200, text="<html>"),
            "no id": lambda request: httpx.Response(200, json={"name": "alyssa", "active": True}),
            "no active": lambda request: httpx.Response(200, json={"id": ACCOUNT["id"], "name": "alyssa"}),
            "active is text": lambda request: httpx.Response(200, json=ACCOUNT | {"active": "true"}),
            "active is null": lambda request: httpx.Response(200, json=ACCOUNT | {"active": None}),
            "active is a number": lambda request: httpx.Response(200, json=ACCOUNT | {"active": 1}),
            "id is a number": lambda request: httpx.Response(200, json=ACCOUNT | {"id": 7}),
            "null body": lambda request: httpx.Response(200, json=None),
            "a list": lambda request: httpx.Response(200, json=[ACCOUNT]),
            "unreachable": unreachable,
        }
        for name, respond in answers.items():
            with self.subTest(name):
                kitaru = FakeKitaru(respond)
                with client(kitaru) as api, self.assertLogs("evalve_backend.auth", level="WARNING"):
                    self.assertEqual(api.get(ME, headers=BEARER).status_code, 503)
                # One request only: a redirect is never followed with the credential.
                self.assertEqual(len(kitaru.requests), 1)

    def test_the_credential_never_reaches_the_logs_or_the_response(self):
        def unreachable(request):
            raise httpx.ConnectError(f"failed sending {request.headers['Authorization']}", request=request)

        answers = [
            lambda request: httpx.Response(200, json=ACCOUNT),
            lambda request: httpx.Response(401, json={"detail": request.headers["Authorization"]}),
            lambda request: httpx.Response(500, text=request.headers["Authorization"]),
            lambda request: httpx.Response(200, text=request.headers["Authorization"]),
            unreachable,
        ]
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, logging.WARNING)
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("test").debug("capture started")
            for respond in answers:
                with client(FakeKitaru(respond)) as api:
                    response = api.get(ME, headers=BEARER)
                    self.assertNotIn(TOKEN, response.text)
                    self.assertNotIn(TOKEN, str(response.headers))
        logged = "\n".join(record.getMessage() + str(record.exc_info or "") for record in captured.records)
        self.assertGreater(len(captured.records), 1)
        self.assertNotIn(TOKEN, logged)


if __name__ == "__main__":
    unittest.main()
