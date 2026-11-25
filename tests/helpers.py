"""Shared pieces for the tests: settings, and a fake Kitaru server."""

import httpx
import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from evalve_backend import database
from evalve_backend.app import create_app
from evalve_backend.settings import Settings

KITARU_URL = "http://kitaru.internal:8000"
TOKEN = "KITKEY_secret-credential-value"
ACCOUNT = {
    "id": "0b0e7c1e-6f0f-4c59-9d7b-1f2f1c8f9a10", "name": "alyssa", "email": "alyssa@example.test",
    "is_service_account": False, "is_admin": True, "active": True, "metadata": {},
}


def settings(**overrides) -> Settings:
    values = {
        "kitaru_url": KITARU_URL, "db_host": "db.internal", "db_user": "evalve_admin",
        "db_password": "database-password-value", "db_ssl_ca": "/run/rds-ca.pem",
    }
    return Settings(**(values | overrides))


def create_database(settings: Settings) -> None:
    """Make a test database. In a deployment, the setup-database command does this."""
    options = database.connection_options(settings, database.MAINTENANCE_DATABASE)
    with psycopg.connect(**options, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.db_name)))


class FakeKitaru:
    """Stands in for the Kitaru server and records every request it receives."""

    def __init__(self, respond=None):
        self.requests: list[httpx.Request] = []
        self.respond = respond or (lambda request: httpx.Response(200, json=ACCOUNT))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def client(kitaru: FakeKitaru | None = None, database_ok: bool = True, app_settings: dict | None = None,
           **app_options) -> TestClient:
    app = create_app(
        settings(**(app_settings or {})),
        kitaru_transport=httpx.MockTransport(kitaru or FakeKitaru()),
        database_ping=lambda _settings: database_ok,
        **app_options,
    )
    return TestClient(app)
