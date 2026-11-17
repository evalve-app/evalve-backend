import os
import unittest
import uuid

import psycopg

from evalve_backend import database
from evalve_backend.__main__ import wait_for_database
from helpers import settings


class FakeConnection:
    def __init__(self, server):
        self.server = server

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else query.as_string(None)
        self.server.queries.append(text)
        if text.startswith("CREATE DATABASE"):
            if self.server.create_error:
                raise self.server.create_error
            self.server.databases.add(text.removeprefix("CREATE DATABASE ").strip('"'))
        return self

    def fetchone(self):
        last = self.server.queries[-1]
        if last == "SELECT 1":
            return (1,)
        return (1,) if self.server.queried_name in self.server.databases else None


class FakeServer:
    def __init__(self, databases=("postgres", "kitaru")):
        self.databases = set(databases)
        self.connections = []
        self.queries = []
        self.create_error = None
        self.connect_error = None
        self.queried_name = "evalve"

    def connect(self, **options):
        self.connections.append(options)
        if self.connect_error:
            raise self.connect_error
        return FakeConnection(self)


class DatabaseTest(unittest.TestCase):
    def test_the_database_is_created_when_the_server_does_not_have_it(self):
        server = FakeServer()
        self.assertTrue(database.ensure_database(settings(), connect=server.connect))
        self.assertEqual(server.databases, {"postgres", "kitaru", "evalve"})
        self.assertEqual(server.queries[-1], 'CREATE DATABASE "evalve"')
        [options] = server.connections
        # CREATE DATABASE cannot run inside a transaction or from the target database.
        self.assertEqual((options["dbname"], options["autocommit"]), ("postgres", True))

    def test_an_existing_database_is_left_alone(self):
        server = FakeServer(databases=("postgres", "kitaru", "evalve"))
        self.assertFalse(database.ensure_database(settings(), connect=server.connect))
        self.assertFalse(any(query.startswith("CREATE") for query in server.queries))

    def test_losing_a_race_to_create_the_database_is_fine(self):
        server = FakeServer()
        server.create_error = psycopg.errors.DuplicateDatabase("already exists")
        self.assertFalse(database.ensure_database(settings(), connect=server.connect))

    def test_connections_verify_the_server_certificate(self):
        server = FakeServer(databases=("postgres", "evalve"))
        self.assertTrue(database.ping(settings(), connect=server.connect))
        [options] = server.connections
        self.assertEqual(options["dbname"], "evalve")
        self.assertEqual((options["sslmode"], options["sslrootcert"]), ("verify-full", "/run/rds-ca.pem"))
        self.assertEqual(options["connect_timeout"], 5)

    def test_ping_is_false_when_the_database_does_not_answer(self):
        server = FakeServer()
        server.connect_error = psycopg.OperationalError("password authentication failed for database-password-value")
        with self.assertLogs("evalve_backend.database", level="WARNING") as captured:
            self.assertFalse(database.ping(settings(), connect=server.connect))
        self.assertNotIn("database-password-value", "\n".join(captured.output))

    def test_startup_retries_until_the_database_server_is_up(self):
        attempts = []

        def ensure(_settings):
            attempts.append(1)
            if len(attempts) < 3:
                raise psycopg.OperationalError("not yet")
            return True

        original, database.ensure_database = database.ensure_database, ensure
        self.addCleanup(setattr, database, "ensure_database", original)
        with self.assertLogs("evalve_backend", level="INFO"):
            wait_for_database(settings(), timeout=60, sleep=lambda _seconds: None, clock=lambda: 0.0)
        self.assertEqual(len(attempts), 3)

    def test_startup_gives_up_after_the_timeout(self):
        def ensure(_settings):
            raise psycopg.OperationalError("down")

        original, database.ensure_database = database.ensure_database, ensure
        self.addCleanup(setattr, database, "ensure_database", original)
        ticks = iter([0.0, 61.0])
        with self.assertRaises(psycopg.OperationalError):
            wait_for_database(settings(), timeout=60, sleep=lambda _seconds: None, clock=lambda: next(ticks))


@unittest.skipUnless(os.environ.get("EVALVE_TEST_DB_HOST"), "set EVALVE_TEST_DB_HOST to test against a real PostgreSQL")
class RealDatabaseTest(unittest.TestCase):
    """Runs against a real PostgreSQL server. CI provides one."""

    def setUp(self):
        self.settings = settings(
            db_host=os.environ["EVALVE_TEST_DB_HOST"],
            db_port=int(os.environ.get("EVALVE_TEST_DB_PORT", "5432")),
            db_user=os.environ.get("EVALVE_TEST_DB_USER", "postgres"),
            db_password=os.environ.get("EVALVE_TEST_DB_PWD", "postgres"),
            db_name=f"evalve_test_{uuid.uuid4().hex[:12]}",
            db_ssl_mode="disable", db_ssl_ca=None,
        )
        self.addCleanup(self.drop)

    def drop(self):
        options = database.connection_options(self.settings, database.MAINTENANCE_DATABASE)
        with psycopg.connect(**options, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{self.settings.db_name}"')

    def test_create_then_ping_then_create_again(self):
        self.assertFalse(database.ping(self.settings))
        self.assertTrue(database.ensure_database(self.settings))
        self.assertTrue(database.ping(self.settings))
        self.assertFalse(database.ensure_database(self.settings))

    def test_a_wrong_password_fails_the_ping(self):
        database.ensure_database(self.settings)
        wrong = settings(**{**self.settings.__dict__, "db_password": "wrong-password"})
        self.assertFalse(database.ping(wrong))


if __name__ == "__main__":
    unittest.main()
