import io
import os
import re
import sys
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import psycopg
from psycopg import sql

from evalve_backend import __main__ as entry
from evalve_backend import setup_database
from evalve_backend.migrations import migrate
from evalve_backend.settings import Settings, SettingsError
from evalve_backend.setup_database import (
    SetupError, SetupSettings, block_kitaru_database, ensure_database, ensure_password, ensure_role,
    run, scram_password, set_up, transfer_objects,
)

ADMIN_PASSWORD = "master-password-value"
ROLE_PASSWORD = "role-password-value"
ENV = {
    "EVALVE_DB_HOST": "db.internal",
    "EVALVE_DB_ADMIN_USER": "evalve_admin",
    "EVALVE_DB_ADMIN_PWD": ADMIN_PASSWORD,
    "EVALVE_DB_USER": "evalve",
    "EVALVE_DB_PWD": ROLE_PASSWORD,
}


class SetupSettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = SetupSettings.from_environment(ENV)
        self.assertEqual((settings.admin_user, settings.role_name), ("evalve_admin", "evalve"))
        self.assertEqual((settings.db_name, settings.kitaru_db_name), ("evalve", "kitaru"))
        self.assertEqual((settings.db_port, settings.db_ssl_mode, settings.db_ssl_ca), (5432, "verify-full", None))

    def test_missing_settings_are_named(self):
        with self.assertRaisesRegex(SettingsError, "^Set EVALVE_DB_ADMIN_USER, EVALVE_DB_ADMIN_PWD.$"):
            SetupSettings.from_environment(
                {key: value for key, value in ENV.items() if "ADMIN" not in key}
            )

    def test_names_must_be_plain_identifiers(self):
        for name in ("EVALVE_DB_USER", "EVALVE_DB_NAME", "EVALVE_KITARU_DB_NAME"):
            for value in ("Evalve", "evalve; drop", '"evalve"', "1evalve"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(SettingsError, f"^{name} must be"):
                        SetupSettings.from_environment(ENV | {name: value})

    def test_the_role_cannot_be_the_master_user(self):
        with self.assertRaisesRegex(SettingsError, "EVALVE_DB_USER must be a role other than"):
            SetupSettings.from_environment(ENV | {"EVALVE_DB_USER": "evalve_admin"})

    def test_the_backend_database_cannot_be_kitaru_database(self):
        with self.assertRaisesRegex(SettingsError, "EVALVE_DB_NAME must be a database other than"):
            SetupSettings.from_environment(ENV | {"EVALVE_DB_NAME": "kitaru"})

    def test_the_role_password_uses_only_plain_characters(self):
        for value in ("pässword", "pass word", "pass'word"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SettingsError, "^EVALVE_DB_PWD must contain only") as raised:
                    SetupSettings.from_environment(ENV | {"EVALVE_DB_PWD": value})
                self.assertNotIn(value, str(raised.exception))

    def test_the_port_must_be_a_number(self):
        with self.assertRaisesRegex(SettingsError, "EVALVE_DB_PORT"):
            SetupSettings.from_environment(ENV | {"EVALVE_DB_PORT": "postgres"})

    def test_passwords_stay_out_of_errors_and_repr(self):
        settings = SetupSettings.from_environment(ENV)
        self.assertNotIn(ADMIN_PASSWORD, repr(settings))
        self.assertNotIn(ROLE_PASSWORD, repr(settings))
        bad = ENV | {"EVALVE_DB_USER": "Bad", "EVALVE_DB_PORT": ROLE_PASSWORD}
        with self.assertRaises(SettingsError) as raised:
            SetupSettings.from_environment(bad)
        self.assertNotIn(ROLE_PASSWORD, str(raised.exception))


class CommandTest(unittest.TestCase):
    def test_the_scrambled_password_has_postgresql_form_and_hides_the_password(self):
        first, second = scram_password(ROLE_PASSWORD), scram_password(ROLE_PASSWORD)
        pattern = r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}:[A-Za-z0-9+/=]{44}"
        self.assertRegex(first, f"^{pattern}$")
        self.assertNotIn(ROLE_PASSWORD, first)
        # Each call picks a new salt, which is why the step logs in to compare.
        self.assertNotEqual(first, second)

    def test_bad_settings_exit_with_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(run({key: value for key, value in ENV.items() if key != "EVALVE_DB_PWD"}), 2)
        self.assertIn("EVALVE_DB_PWD", stderr.getvalue())

    def test_an_unreachable_database_exits_with_1_and_prints_only_the_kind(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            # Nothing listens on port 1, so the connection is refused at once.
            self.assertEqual(run(ENV | {"EVALVE_DB_HOST": "127.0.0.1", "EVALVE_DB_PORT": "1"}), 1)
        self.assertEqual(stderr.getvalue().strip(), "The database setup failed (OperationalError).")

    def test_the_command_runs_the_setup_and_not_the_server(self):
        with patch.object(sys, "argv", ["evalve-backend", "setup-database"]), \
                patch.object(setup_database, "run", return_value=0) as setup, \
                patch.object(entry, "uvicorn") as server:
            self.assertEqual(entry.main(), 0)
        setup.assert_called_once_with()
        server.run.assert_not_called()


@unittest.skipUnless(os.environ.get("EVALVE_TEST_DB_HOST"), "set EVALVE_TEST_DB_HOST to test against a real PostgreSQL")
class RealDatabaseTest(unittest.TestCase):
    """Runs the step against a real PostgreSQL server. CI provides one.

    The step runs as a test admin that, like the master user on RDS, can create
    databases and roles but is not a superuser. A superuser skips the checks
    that the master user on RDS must pass.
    """

    ADMIN_PASSWORD = "test-admin-password"

    def setUp(self):
        suffix = uuid.uuid4().hex[:12]
        self.server = {
            "host": os.environ["EVALVE_TEST_DB_HOST"],
            "port": int(os.environ.get("EVALVE_TEST_DB_PORT", "5432")),
        }
        self.env = {
            "EVALVE_DB_HOST": self.server["host"],
            "EVALVE_DB_PORT": str(self.server["port"]),
            "EVALVE_DB_ADMIN_USER": f"test_admin_{suffix}",
            "EVALVE_DB_ADMIN_PWD": self.ADMIN_PASSWORD,
            "EVALVE_DB_USER": f"test_role_{suffix}",
            "EVALVE_DB_PWD": "test-role-password",
            "EVALVE_DB_NAME": f"test_evalve_{suffix}",
            "EVALVE_KITARU_DB_NAME": f"test_kitaru_{suffix}",
            "EVALVE_DB_SSL_MODE": "disable",
        }
        self.settings = SetupSettings.from_environment(self.env)
        self.group = f"test_group_{suffix}"
        with self.superuser() as connection:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE PASSWORD {}").format(
                    sql.Identifier(self.settings.admin_user), sql.Literal(self.ADMIN_PASSWORD)
                )
            )
        self.addCleanup(self.drop_roles)
        # Cleanups run last first, so the databases go before the roles that own them.
        self.addCleanup(self.drop_databases)

    def superuser(self, database="postgres"):
        return psycopg.connect(
            **self.server, dbname=database, sslmode="disable", autocommit=True,
            user=os.environ.get("EVALVE_TEST_DB_USER", "postgres"),
            password=os.environ.get("EVALVE_TEST_DB_PWD", "postgres"),
        )

    def admin(self, database="postgres"):
        return psycopg.connect(
            **self.server, dbname=database, sslmode="disable", autocommit=True,
            user=self.settings.admin_user, password=self.ADMIN_PASSWORD,
        )

    def drop_roles(self):
        with self.superuser() as connection:
            for name in (self.group, self.settings.role_name, self.settings.admin_user):
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))

    def drop_databases(self):
        with self.superuser() as connection:
            for name in (self.settings.db_name, self.settings.kitaru_db_name):
                connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))

    def database_owner(self):
        with self.superuser() as connection:
            row = connection.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (self.settings.db_name,)
            ).fetchone()
            return row[0] if row else None

    def set_role_password(self):
        # The step sets the password in a later piece. Set it here so the role can log in.
        with self.superuser() as connection:
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(self.settings.role_name), sql.Literal(self.settings.role_password)
                )
            )

    def connect_as_role(self, database):
        return psycopg.connect(
            **self.server, dbname=database, sslmode="disable", autocommit=True,
            user=self.settings.role_name, password=self.settings.role_password,
        )

    def create_kitaru_database(self):
        # On RDS the master user owns Kitaru's database.
        with self.admin() as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.settings.kitaru_db_name)))

    def backend_settings(self, user, password):
        return Settings(
            kitaru_url="http://kitaru.internal:8000", db_host=self.server["host"], db_port=self.server["port"],
            db_user=user, db_password=password, db_name=self.settings.db_name,
            db_ssl_mode="disable", db_ssl_ca=None,
        )

    def objects_not_owned_by_the_role(self):
        """Every object in the backend's database that another user owns, of any kind."""
        user_schema = "nspname <> 'information_schema' AND left(nspname, 3) <> 'pg_'"
        with self.superuser(self.settings.db_name) as connection:
            return connection.execute(
                f"""
                SELECT 'schema ' || nspname FROM pg_namespace
                 WHERE {user_schema} AND pg_get_userbyid(nspowner) NOT IN (%(role)s, 'pg_database_owner')
                UNION ALL
                SELECT 'relation ' || relname FROM pg_class JOIN pg_namespace n ON n.oid = relnamespace
                 WHERE {user_schema} AND pg_get_userbyid(relowner) <> %(role)s
                UNION ALL
                SELECT 'function ' || proname FROM pg_proc JOIN pg_namespace n ON n.oid = pronamespace
                 WHERE {user_schema} AND pg_get_userbyid(proowner) <> %(role)s
                UNION ALL
                SELECT 'type ' || typname FROM pg_type JOIN pg_namespace n ON n.oid = typnamespace
                 WHERE {user_schema} AND pg_get_userbyid(typowner) <> %(role)s
                """,
                {"role": self.settings.role_name},
            ).fetchall()

    def role_rights(self):
        with self.superuser() as connection:
            return connection.execute(
                "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
                (self.settings.role_name,),
            ).fetchone()

    def test_the_first_run_creates_the_role_and_a_second_run_changes_nothing(self):
        with self.admin() as connection:
            self.assertEqual(
                ensure_role(connection, self.settings),
                ["created the role", "let the master user act as the role"],
            )
            self.assertEqual(ensure_role(connection, self.settings), [])
        self.assertEqual(self.role_rights(), (False, True, False, False))

    def test_the_master_user_may_act_as_the_role(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.settings.role_name)))
            self.assertEqual(connection.execute("SELECT current_user").fetchone()[0], self.settings.role_name)

    def test_extra_rights_are_removed(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
        with self.superuser() as connection:
            connection.execute(
                sql.SQL("ALTER ROLE {} NOLOGIN CREATEDB CREATEROLE").format(sql.Identifier(self.settings.role_name))
            )
        with self.admin() as connection:
            self.assertEqual(
                ensure_role(connection, self.settings),
                ["fixed the rights of the role: it can log in and cannot create a database or a role"],
            )
        self.assertEqual(self.role_rights(), (False, True, False, False))

    def test_a_superuser_role_stops_the_step(self):
        with self.superuser() as connection:
            connection.execute(sql.SQL("CREATE ROLE {} SUPERUSER").format(sql.Identifier(self.settings.role_name)))
        with self.admin() as connection:
            with self.assertRaisesRegex(SetupError, "is a superuser"):
                ensure_role(connection, self.settings)
        self.assertEqual(self.role_rights()[0], True)


    def test_a_missing_database_is_created_for_the_role(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            self.assertEqual(ensure_database(connection, self.settings), ["created the database"])
            self.assertEqual(ensure_database(connection, self.settings), [])
        self.assertEqual(self.database_owner(), self.settings.role_name)

    def test_a_database_of_the_master_user_is_handed_to_the_role(self):
        # A live deployment: the backend made its database as the master user.
        with self.admin() as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.settings.db_name)))
        self.assertEqual(self.database_owner(), self.settings.admin_user)
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            self.assertEqual(ensure_database(connection, self.settings), ["made the role the owner of the database"])
            self.assertEqual(ensure_database(connection, self.settings), [])
        self.assertEqual(self.database_owner(), self.settings.role_name)

    def test_the_role_can_create_tables_in_its_database(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            ensure_database(connection, self.settings)
        self.set_role_password()
        with self.connect_as_role(self.settings.db_name) as connection:
            connection.execute("CREATE TABLE migration_check (id integer)")
            connection.execute("DROP TABLE migration_check")


    def test_the_role_cannot_connect_to_kitaru_database(self):
        self.create_kitaru_database()
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            ensure_database(connection, self.settings)
            self.assertEqual(
                block_kitaru_database(connection, self.settings),
                [f"blocked the role from {self.settings.kitaru_db_name}"],
            )
            self.assertEqual(block_kitaru_database(connection, self.settings), [])
        self.set_role_password()
        with self.assertRaisesRegex(psycopg.OperationalError, "permission denied"):
            self.connect_as_role(self.settings.kitaru_db_name).close()
        # The role still reaches its own database, and Kitaru's user still reaches Kitaru's.
        self.connect_as_role(self.settings.db_name).close()
        self.admin(self.settings.kitaru_db_name).close()

    def test_nothing_is_blocked_when_kitaru_database_does_not_exist(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            self.assertEqual(block_kitaru_database(connection, self.settings), [])

    def test_a_way_in_through_another_role_stops_the_step(self):
        self.create_kitaru_database()
        with self.admin() as connection:
            ensure_role(connection, self.settings)
        with self.superuser() as connection:
            group, role = sql.Identifier(self.group), sql.Identifier(self.settings.role_name)
            connection.execute(sql.SQL("CREATE ROLE {}").format(group))
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(self.settings.kitaru_db_name), group
                )
            )
            connection.execute(sql.SQL("GRANT {} TO {}").format(group, role))
        with self.admin() as connection:
            with self.assertRaisesRegex(SetupError, "can still connect"):
                block_kitaru_database(connection, self.settings)


    def test_a_live_database_is_handed_to_the_role_with_its_loops(self):
        # A live deployment: the backend made its database and tables as the master user.
        with self.admin() as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.settings.db_name)))
        migrate(self.backend_settings(self.settings.admin_user, self.ADMIN_PASSWORD))
        with self.admin(self.settings.db_name) as connection:
            connection.execute(
                "INSERT INTO loops (id, owner_account_id, name, slug, agent_id, config, preflight,"
                " idempotency_key, request_hash) VALUES ('loop-1', 'account-1', 'Loop', 'loop', 'agent-1',"
                " '{}', '{}', 'key-1', 'hash-1')"
            )
        self.assertNotEqual(self.objects_not_owned_by_the_role(), [])

        with self.admin() as connection:
            ensure_role(connection, self.settings)
            ensure_database(connection, self.settings)
        with self.admin(self.settings.db_name) as connection:
            self.assertEqual(transfer_objects(connection, self.settings), ["made the role the owner of 4 objects"])
            self.assertEqual(transfer_objects(connection, self.settings), [])
        self.assertEqual(self.objects_not_owned_by_the_role(), [])

        self.set_role_password()
        with self.connect_as_role(self.settings.db_name) as connection:
            self.assertEqual(connection.execute("SELECT id FROM loops").fetchall(), [("loop-1",)])
        self.assertEqual(migrate(self.backend_settings(self.settings.role_name, self.settings.role_password)), ())

    def test_a_new_database_has_nothing_to_hand_over(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            ensure_database(connection, self.settings)
        with self.admin(self.settings.db_name) as connection:
            self.assertEqual(transfer_objects(connection, self.settings), [])


    def test_the_password_is_set_once_and_follows_a_new_value(self):
        with self.admin() as connection:
            ensure_role(connection, self.settings)
            ensure_database(connection, self.settings)
        self.assertEqual(ensure_password(self.settings), ["set the password of the role"])
        self.connect_as_role(self.settings.db_name).close()
        self.assertEqual(ensure_password(self.settings), [])

        old = self.settings
        self.settings = SetupSettings.from_environment(self.env | {"EVALVE_DB_PWD": "a-new-password-value"})
        self.assertEqual(ensure_password(self.settings), ["set the password of the role"])
        self.connect_as_role(self.settings.db_name).close()
        self.settings = old
        with self.assertRaisesRegex(psycopg.OperationalError, "password authentication failed"):
            self.connect_as_role(self.settings.db_name).close()

    def test_the_command_sets_everything_up_then_changes_nothing(self):
        self.create_kitaru_database()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(run(self.env), 0)
            self.assertEqual(run(self.env), 0)
        first, second = stdout.getvalue().splitlines()
        self.assertEqual(
            first,
            "Database setup: created the role; let the master user act as the role; created the database;"
            f" blocked the role from {self.settings.kitaru_db_name}; set the password of the role.",
        )
        self.assertEqual(second, "Database setup: no changes.")
        self.assertNotIn(self.settings.role_password, stdout.getvalue())
        # The backend can log in with the secret's password and migrate its database.
        self.assertNotEqual(migrate(self.backend_settings(self.settings.role_name, self.settings.role_password)), ())


if __name__ == "__main__":
    unittest.main()
