import logging
import unittest

import httpx

from helpers import ACCOUNT, KITARU_URL, TOKEN, FakeKitaru, client

ME = "/evalve/v1/me"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
SESSION_TOKEN = "eyJhbGciOiJIUzI1NiJ9.session-token-value.c2lnbmF0dXJl"
CSRF_TOKEN = "csrf-token-value_0123"
SESSION = {"Cookie": f"evalve_session={SESSION_TOKEN}", "X-CSRF-Token": CSRF_TOKEN}
DASHBOARD_ACCOUNT = ACCOUNT | {"id": "5d2c8a40-0000-4000-8000-00000000d00d", "name": "dashboard-user"}


def kitaru_with_sessions(cookie_name: str = "evalve_session") -> FakeKitaru:
    """Answers as Kitaru 0.27.6 does: the header wins, and a cookie session needs its CSRF token."""
    def respond(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            ok = request.headers["authorization"] == BEARER["Authorization"]
            return httpx.Response(200, json=ACCOUNT) if ok else httpx.Response(401, json={"detail": "no"})
        if request.headers.get("cookie") != f"{cookie_name}={SESSION_TOKEN}":
            return httpx.Response(401, json={"detail": "Missing bearer credential."})
        if request.headers.get("x-csrf-token") != CSRF_TOKEN:
            return httpx.Response(401, json={"detail": "Missing or invalid CSRF token."})
        return httpx.Response(200, json=DASHBOARD_ACCOUNT)

    return FakeKitaru(respond)


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


class DashboardSessionTest(unittest.TestCase):
    def test_a_session_cookie_with_its_csrf_token_returns_the_account(self):
        kitaru = kitaru_with_sessions()
        with client(kitaru) as api:
            response = api.get(ME, headers=SESSION)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": DASHBOARD_ACCOUNT["id"], "name": "dashboard-user"})
        [request] = kitaru.requests
        self.assertEqual(str(request.url), f"{KITARU_URL}/api/v1/accounts/me")
        self.assertEqual(request.headers["cookie"], f"evalve_session={SESSION_TOKEN}")
        self.assertEqual(request.headers["x-csrf-token"], CSRF_TOKEN)
        # The cookie stays a cookie. Kitaru skips its CSRF check for an Authorization header.
        self.assertNotIn("authorization", request.headers)

    def test_only_the_session_cookie_is_passed_to_kitaru(self):
        kitaru = kitaru_with_sessions()
        cookies = f"theme=dark; evalve_session={SESSION_TOKEN}; tracking=abc123"
        with client(kitaru) as api:
            self.assertEqual(api.get(ME, headers=SESSION | {"Cookie": cookies}).status_code, 200)
        [request] = kitaru.requests
        self.assertEqual(request.headers["cookie"], f"evalve_session={SESSION_TOKEN}")

    def test_a_session_with_a_missing_or_wrong_csrf_token_is_refused(self):
        for name, headers in (
            ("missing", {"Cookie": SESSION["Cookie"]}),
            ("wrong", SESSION | {"X-CSRF-Token": "another-token"}),
        ):
            with self.subTest(name):
                kitaru = kitaru_with_sessions()
                with client(kitaru) as api:
                    response = api.get(ME, headers=headers)
                self.assertEqual(response.status_code, 401)
                # Kitaru made the decision. The backend has no CSRF check of its own.
                [request] = kitaru.requests
                self.assertEqual(request.headers.get("x-csrf-token"), headers.get("X-CSRF-Token"))

    def test_an_invalid_or_expired_session_is_refused(self):
        kitaru = kitaru_with_sessions()
        with client(kitaru) as api:
            response = api.get(ME, headers=SESSION | {"Cookie": "evalve_session=expired.session.token"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(kitaru.requests), 1)

    def test_the_authorization_header_wins_over_the_cookie(self):
        kitaru = kitaru_with_sessions()
        with client(kitaru) as api:
            response = api.get(ME, headers=BEARER | {"Cookie": "evalve_session=invalid.session", "X-CSRF-Token": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "alyssa")
        [request] = kitaru.requests
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("x-csrf-token", request.headers)

    def test_an_authorization_header_that_is_refused_is_not_replaced_by_the_cookie(self):
        kitaru = kitaru_with_sessions()
        with client(kitaru) as api:
            # Kitaru refuses the key. The valid session is not a second try.
            self.assertEqual(api.get(ME, headers=SESSION | {"Authorization": "Bearer wrong-key"}).status_code, 401)
            self.assertNotIn("cookie", kitaru.requests[-1].headers)
            # Not a Bearer header: refused before Kitaru is asked.
            self.assertEqual(api.get(ME, headers=SESSION | {"Authorization": "Basic dXNlcjpwYXNz"}).status_code, 401)
        self.assertEqual(len(kitaru.requests), 1)

    def test_an_empty_cookie_name_turns_cookie_sign_in_off(self):
        kitaru = kitaru_with_sessions()
        with client(kitaru, app_settings={"session_cookie_name": ""}) as api:
            self.assertEqual(api.get(ME, headers=SESSION).status_code, 401)
            self.assertEqual(kitaru.requests, [])
            self.assertEqual(api.get(ME, headers=BEARER).status_code, 200)

    def test_the_cookie_name_is_the_setting(self):
        kitaru = kitaru_with_sessions("kitaru_session")
        with client(kitaru, app_settings={"session_cookie_name": "kitaru_session"}) as api:
            self.assertEqual(api.get(ME, headers=SESSION).status_code, 401)
            self.assertEqual(kitaru.requests, [])
            named = SESSION | {"Cookie": f"kitaru_session={SESSION_TOKEN}"}
            self.assertEqual(api.get(ME, headers=named).status_code, 200)
        self.assertEqual(kitaru.requests[0].headers["cookie"], f"kitaru_session={SESSION_TOKEN}")

    def test_a_session_that_cannot_be_passed_on_safely_is_refused_without_asking_kitaru(self):
        kitaru = kitaru_with_sessions()
        cases = {
            "empty cookie": {"Cookie": "evalve_session=", "X-CSRF-Token": CSRF_TOKEN},
            "cookie with a space": {"Cookie": 'evalve_session="a b"', "X-CSRF-Token": CSRF_TOKEN},
            "cookie with a backslash": {"Cookie": "evalve_session=a\\b", "X-CSRF-Token": CSRF_TOKEN},
            "cookie too long": {"Cookie": "evalve_session=" + "x" * 5000, "X-CSRF-Token": CSRF_TOKEN},
            "cookie outside ascii": {"Cookie": "evalve_session=caf\xe9".encode("latin-1"), "X-CSRF-Token": CSRF_TOKEN},
            "csrf with a space": SESSION | {"X-CSRF-Token": "a b"},
            "csrf with a semicolon": SESSION | {"X-CSRF-Token": "a;b"},
            "csrf too long": SESSION | {"X-CSRF-Token": "x" * 5000},
            "empty csrf": SESSION | {"X-CSRF-Token": ""},
        }
        with client(kitaru) as api:
            for name, headers in cases.items():
                with self.subTest(name):
                    self.assertEqual(api.get(ME, headers=headers).status_code, 401)
        self.assertEqual(kitaru.requests, [])

    def test_a_cookie_from_a_kitaru_answer_never_reaches_another_caller(self):
        # One HTTP client serves every caller, so it must not keep cookies.
        def respond(request: httpx.Request) -> httpx.Response:
            leak = {"Set-Cookie": f"evalve_session={SESSION_TOKEN}; Path=/"}
            return httpx.Response(200, json=ACCOUNT, headers=leak)

        kitaru = FakeKitaru(respond)
        with client(kitaru) as api:
            first = api.get(ME, headers=SESSION)
            second = api.get(ME, headers=BEARER)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertNotIn("cookie", kitaru.requests[1].headers)
        self.assertNotIn("set-cookie", first.headers)

    def test_the_session_never_reaches_the_logs_or_the_response(self):
        def unreachable(request):
            raise httpx.ConnectError(f"failed sending {request.headers['Cookie']} {request.headers['X-CSRF-Token']}", request=request)

        def echo(status):
            return lambda request: httpx.Response(status, text=f"{request.headers['Cookie']} {request.headers['X-CSRF-Token']}")

        answers = [
            lambda request: httpx.Response(200, json=DASHBOARD_ACCOUNT),
            lambda request: httpx.Response(401, json={"detail": request.headers["Cookie"] + request.headers["X-CSRF-Token"]}),
            echo(500), echo(200), unreachable,
        ]
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, logging.WARNING)
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("test").debug("capture started")
            for respond in answers:
                with client(FakeKitaru(respond)) as api:
                    response = api.get(ME, headers=SESSION)
                    for secret in (SESSION_TOKEN, CSRF_TOKEN):
                        self.assertNotIn(secret, response.text)
                        self.assertNotIn(secret, str(response.headers))
        logged = "\n".join(record.getMessage() + str(record.exc_info or "") for record in captured.records)
        self.assertGreater(len(captured.records), 1)
        self.assertNotIn(SESSION_TOKEN, logged)
        self.assertNotIn(CSRF_TOKEN, logged)


if __name__ == "__main__":
    unittest.main()
