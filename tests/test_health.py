import unittest

import httpx
from fastapi.testclient import TestClient

from evalve_backend import __version__
from evalve_backend.app import create_app
from helpers import FakeKitaru, client, settings

IMAGE = "ghcr.io/evalve-app/evalve-backend@sha256:" + "ab" * 32


class HealthTest(unittest.TestCase):
    def test_health_reports_a_reachable_database(self):
        kitaru = FakeKitaru()
        with client(kitaru) as api:
            response = api.get("/evalve/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"status": "ok", "database": "ok", "version": __version__, "image": None}
        )
        # Health needs no sign-in and never calls Kitaru.
        self.assertEqual(kitaru.requests, [])

    def test_health_fails_when_the_database_does_not_answer(self):
        with client(database_ok=False) as api:
            response = api.get("/evalve/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["database"], "unavailable")

    def test_health_names_the_image_it_was_started_from(self):
        for database_ok, status in ((True, 200), (False, 503)):
            with self.subTest(database_ok=database_ok):
                app = create_app(
                    settings(image=IMAGE),
                    kitaru_transport=httpx.MockTransport(FakeKitaru()),
                    database_ping=lambda _settings: database_ok,
                )
                with TestClient(app) as api:
                    response = api.get("/evalve/health")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["image"], IMAGE)

    def test_nothing_is_served_outside_the_evalve_prefix(self):
        with client() as api:
            for path in ("/", "/health", "/v1/me", "/api/v1/accounts/me", "/docs"):
                with self.subTest(path=path):
                    self.assertEqual(api.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
