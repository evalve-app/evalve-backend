import os
import socket
import unittest
import uuid
from unittest.mock import Mock, patch

import psycopg

from evalve_backend import database
from evalve_backend.__main__ import wait_for_database
from helpers import create_database, settings


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeServer:
    def __init__(self):
        self.connections = []
        self.connect_error = None

    def connect(self, **options):
        self.connections.append(options)
        if self.connect_error:
            raise self.connect_error
        return FakeConnection()


class DatabaseTest(unittest.TestCase):
    def test_check_connection_opens_the_backend_database(self):
        server = FakeServer()
        database.check_connection(settings(), connect=server.connect)
        [options] = server.connections
        # The backend's own database, not the maintenance database it used to create it from.
        self.assertEqual(options["dbname"], "evalve")
        self.assertNotIn("autocommit", options)

    def test_check_connection_raises_when_the_database_does_not_answer(self):
        server = FakeServer()
        server.connect_error = psycopg.OperationalError("database \"evalve\" does not exist")
        with self.assertRaises(psycopg.OperationalError):
            database.check_connection(settings(), connect=server.connect)

    def test_connections_verify_the_server_certificate(self):
        connect = Mock()
        with patch.object(database, "_query_answers", return_value=True):
            self.assertTrue(database.ping(settings(), connect=connect))
        options = connect.call_args.kwargs
        self.assertEqual(options["dbname"], "evalve")
        self.assertEqual((options["sslmode"], options["sslrootcert"]), ("verify-full", "/run/rds-ca.pem"))
        self.assertEqual(options["connect_timeout"], 5)

    def test_ping_is_false_when_the_database_does_not_answer(self):
        server = FakeServer()
        server.connect_error = psycopg.OperationalError("password authentication failed for database-password-value")
        with self.assertLogs("evalve_backend.database", level="WARNING") as captured:
            self.assertFalse(database.ping(settings(), connect=server.connect))
        self.assertNotIn("database-password-value", "\n".join(captured.output))

    def test_ping_closes_a_connection_stalled_after_sending_the_query(self):
        client, server = socket.socketpair()
        with client, server:
            connection = Mock()
            connection.close.side_effect = client.close
            connection.pgconn.socket = client.fileno()
            connection.pgconn.flush.return_value = 0
            connection.pgconn.is_busy.return_value = 1
            with patch.object(database, "HEALTH_QUERY_TIMEOUT", 0.01):
                with self.assertLogs("evalve_backend.database", level="WARNING") as captured:
                    self.assertFalse(database.ping(settings(), connect=Mock(return_value=connection)))

            connection.pgconn.send_query.assert_called_once_with(b"SELECT 1")
            connection.pgconn.get_result.assert_not_called()
            connection.close.assert_called_once_with()
            self.assertEqual(client.fileno(), -1)
            self.assertIn("TimeoutError", captured.output[0])

    def test_startup_retries_until_the_database_server_is_up(self):
        attempts = []

        def check(_settings):
            attempts.append(1)
            if len(attempts) < 3:
                raise psycopg.OperationalError("not yet")

        original, database.check_connection = database.check_connection, check
        self.addCleanup(setattr, database, "check_connection", original)
        with self.assertLogs("evalve_backend", level="WARNING"):
            wait_for_database(settings(), timeout=60, sleep=lambda _seconds: None, clock=lambda: 0.0)
        self.assertEqual(len(attempts), 3)

    def test_startup_gives_up_after_the_timeout(self):
        def check(_settings):
            raise psycopg.OperationalError("down")

        original, database.check_connection = database.check_connection, check
        self.addCleanup(setattr, database, "check_connection", original)
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

    def test_check_connection_needs_the_database(self):
        with self.assertRaises(psycopg.OperationalError):
            database.check_connection(self.settings)
        self.assertFalse(database.ping(self.settings))
        create_database(self.settings)
        database.check_connection(self.settings)
        self.assertTrue(database.ping(self.settings))

    def test_a_wrong_password_fails_the_ping(self):
        create_database(self.settings)
        wrong = settings(**{**self.settings.__dict__, "db_password": "wrong-password"})
        self.assertFalse(database.ping(wrong))


if __name__ == "__main__":
    unittest.main()
