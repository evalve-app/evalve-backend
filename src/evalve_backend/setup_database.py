"""The database setup step: give the backend its own PostgreSQL role.

The deployment runs this once before the backend starts, with the credentials
of the master user (ADR 0005 of evalve-deploy). The running backend then holds
only the credentials of the role.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from evalve_backend.settings import SettingsError, port_setting

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# PostgreSQL normalizes other characters before it scrambles a password, and
# scram_password does not, so such a password could not log in. evalve up
# makes the password from these characters only.
ROLE_PASSWORD = re.compile(r"[A-Za-z0-9_-]+")


class SetupError(Exception):
    """The database is in a state that the step must not change by itself."""


@dataclass(frozen=True)
class SetupSettings:
    db_host: str
    # The master user. The step connects with it to make and change the role.
    admin_user: str
    admin_password: str = field(repr=False)
    # The role of the backend: the same values that the running backend gets.
    role_name: str
    role_password: str = field(repr=False)
    db_port: int = 5432
    db_name: str = "evalve"
    kitaru_db_name: str = "kitaru"
    db_ssl_mode: str = "verify-full"
    db_ssl_ca: str | None = None

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "SetupSettings":
        env = os.environ if env is None else env
        required = ("EVALVE_DB_HOST", "EVALVE_DB_ADMIN_USER", "EVALVE_DB_ADMIN_PWD", "EVALVE_DB_USER", "EVALVE_DB_PWD")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise SettingsError("Set " + ", ".join(missing) + ".")
        names = {
            "EVALVE_DB_USER": env["EVALVE_DB_USER"].strip(),
            "EVALVE_DB_NAME": env.get("EVALVE_DB_NAME", "evalve"),
            "EVALVE_KITARU_DB_NAME": env.get("EVALVE_KITARU_DB_NAME", "kitaru"),
        }
        for name, value in names.items():
            if not IDENTIFIER.fullmatch(value):
                raise SettingsError(f"{name} must be a lowercase PostgreSQL identifier.")
        admin_user = env["EVALVE_DB_ADMIN_USER"].strip()
        if names["EVALVE_DB_USER"] == admin_user:
            raise SettingsError("EVALVE_DB_USER must be a role other than EVALVE_DB_ADMIN_USER.")
        if names["EVALVE_DB_NAME"] == names["EVALVE_KITARU_DB_NAME"]:
            raise SettingsError("EVALVE_DB_NAME must be a database other than EVALVE_KITARU_DB_NAME.")
        if not ROLE_PASSWORD.fullmatch(env["EVALVE_DB_PWD"]):
            raise SettingsError("EVALVE_DB_PWD must contain only letters, digits, '-' and '_'.")
        return cls(
            db_host=env["EVALVE_DB_HOST"].strip(),
            admin_user=admin_user,
            admin_password=env["EVALVE_DB_ADMIN_PWD"],
            role_name=names["EVALVE_DB_USER"],
            role_password=env["EVALVE_DB_PWD"],
            db_port=port_setting(env, "EVALVE_DB_PORT", 5432),
            db_name=names["EVALVE_DB_NAME"],
            kitaru_db_name=names["EVALVE_KITARU_DB_NAME"],
            db_ssl_mode=env.get("EVALVE_DB_SSL_MODE", "verify-full"),
            db_ssl_ca=env.get("EVALVE_DB_SSL_CA") or None,
        )


def ensure_role(connection, settings: SetupSettings) -> list[str]:
    """Make the backend's role exist with a login and no right to create a database or a role.

    The connection belongs to the master user. Returns what this call changed.
    The password is set later, in one place, as a scrambled value.
    """
    changes = []
    role = sql.Identifier(settings.role_name)
    row = connection.execute(
        "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
        (settings.role_name,),
    ).fetchone()
    if row is None:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOCREATEDB NOCREATEROLE").format(role))
        changes.append("created the role")
    elif row[0]:
        # Only a superuser can remove this, and the master user on RDS is not one.
        raise SetupError(
            f"The role {settings.role_name} is a superuser. A superuser must run "
            f"ALTER ROLE {settings.role_name} NOSUPERUSER, then the step can run again."
        )
    elif row[1:] != (True, False, False):
        connection.execute(sql.SQL("ALTER ROLE {} LOGIN NOCREATEDB NOCREATEROLE").format(role))
        changes.append("fixed the rights of the role: it can log in and cannot create a database or a role")
    # Since PostgreSQL 16 the master user can make the role the owner of a
    # database or an object only when it may act as the role (SET ROLE).
    allowed = connection.execute(
        "SELECT pg_has_role(current_user, %s, 'SET')", (settings.role_name,)
    ).fetchone()[0]
    if not allowed:
        connection.execute(sql.SQL("GRANT {} TO CURRENT_USER WITH SET TRUE").format(role))
        changes.append("let the master user act as the role")
    return changes


def ensure_database(connection, settings: SetupSettings) -> list[str]:
    """Make the backend's database exist with the role as its owner.

    The connection belongs to the master user, on the maintenance database.
    The master user must already be allowed to act as the role (ensure_role).
    Returns what this call changed.
    """
    database, role = sql.Identifier(settings.db_name), sql.Identifier(settings.role_name)
    owner = _database_owner(connection, settings.db_name)
    if owner is None:
        try:
            connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(database, role))
            return ["created the database"]
        except psycopg.errors.DuplicateDatabase:
            # Another run created it between the check and the create.
            owner = _database_owner(connection, settings.db_name)
    if owner == settings.role_name:
        return []
    connection.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, role))
    return ["made the role the owner of the database"]


def _database_owner(connection, name: str) -> str | None:
    row = connection.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (name,)
    ).fetchone()
    return row[0] if row else None


def block_kitaru_database(connection, settings: SetupSettings) -> list[str]:
    """Keep the role from connecting to Kitaru's database.

    The connection belongs to the master user, on the maintenance database.
    Kitaru keeps its access: it connects as the master user, which owns its
    database. Does nothing when that database does not exist. Returns what
    this call changed.
    """
    if not _can_connect(connection, settings.role_name, settings.kitaru_db_name):
        return []
    # Each login may connect to each database through PUBLIC, so PUBLIC loses it too.
    database = sql.Identifier(settings.kitaru_db_name)
    connection.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(database))
    connection.execute(
        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(database, sql.Identifier(settings.role_name))
    )
    if _can_connect(connection, settings.role_name, settings.kitaru_db_name):
        raise SetupError(
            f"The role {settings.role_name} can still connect to {settings.kitaru_db_name} "
            "through another role it belongs to. Remove that membership or that role's CONNECT right."
        )
    return [f"blocked the role from {settings.kitaru_db_name}"]


def _can_connect(connection, role: str, database: str) -> bool:
    # False also when the database does not exist, so there is nothing to block.
    row = connection.execute(
        "SELECT has_database_privilege(%s, oid, 'CONNECT') FROM pg_database WHERE datname = %s",
        (role, database),
    ).fetchone()
    return bool(row and row[0])


# PostgreSQL's own schemas. Each other schema in the database belongs to the role.
_USER_SCHEMA = "{0} <> 'information_schema' AND left({0}, 3) <> 'pg_'"
# Kinds of objects in pg_class that have their own owner. An index always has
# the owner of its table. A sequence that a column owns moves with its table,
# so sequences go in a second pass, after the tables.
_OWNED_KINDS = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE"}


def transfer_objects(connection, settings: SetupSettings) -> list[str]:
    """Make the role the owner of each schema, table, view and sequence in its database.

    The connection belongs to the master user, on the backend's database. The
    master user must already be allowed to act as the role (ensure_role). Each
    object changes owner by its name: REASSIGN OWNED would also move Kitaru's
    database. Returns what this call changed.
    """
    role = sql.Identifier(settings.role_name)
    schemas = connection.execute(
        f"SELECT nspname FROM pg_namespace WHERE {_USER_SCHEMA.format('nspname')}"
        # Since PostgreSQL 15 the schema public belongs to whoever owns the database.
        " AND pg_get_userbyid(nspowner) NOT IN (%s, 'pg_database_owner') ORDER BY nspname",
        (settings.role_name,),
    ).fetchall()
    for (name,) in schemas:
        connection.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(sql.Identifier(name), role))
    moved = len(schemas)
    for kinds in (["r", "p", "v", "m"], ["S"]):
        relations = connection.execute(
            "SELECT n.nspname, c.relname, c.relkind::text FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            f" WHERE {_USER_SCHEMA.format('n.nspname')}"
            " AND c.relkind = ANY(%s::\"char\"[]) AND pg_get_userbyid(c.relowner) <> %s"
            " ORDER BY n.nspname, c.relname",
            (kinds, settings.role_name),
        ).fetchall()
        for schema, name, kind in relations:
            connection.execute(
                sql.SQL("ALTER {} {} OWNER TO {}").format(
                    sql.SQL(_OWNED_KINDS[kind]), sql.Identifier(schema, name), role
                )
            )
        moved += len(relations)
    return [f"made the role the owner of {moved} objects"] if moved else []


def scram_password(password: str) -> str:
    """Scramble a password the way PostgreSQL stores it (SCRAM-SHA-256, RFC 5802).

    PostgreSQL stores a value in this form as it is, so the real password is
    never part of a command, and a failed command cannot write it to a log.
    """
    salt = secrets.token_bytes(16)
    iterations = 4096
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    stored_key = hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    salt_text, stored_text, server_text = (base64.b64encode(value).decode() for value in (salt, stored_key, server_key))
    return f"SCRAM-SHA-256${iterations}:{salt_text}${stored_text}:{server_text}"


def connection_options(settings: SetupSettings, database: str, user: str, password: str) -> dict:
    options = {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": user,
        "password": password,
        "dbname": database,
        "sslmode": settings.db_ssl_mode,
        "connect_timeout": 5,
        "application_name": "evalve-backend-setup",
    }
    if settings.db_ssl_ca:
        options["sslrootcert"] = settings.db_ssl_ca
    return options


def ensure_password(settings: SetupSettings, connect=psycopg.connect) -> list[str]:
    """Make PostgreSQL accept the role's password from the secret.

    The secret is the source of truth. If the role can log in with it, nothing
    changes. If not (no password yet, a new value in the secret, or an old one
    from a restore), PostgreSQL gets the secret's password, and the role must
    then log in. Returns what this call changed.
    """
    role_login = connection_options(settings, settings.db_name, settings.role_name, settings.role_password)
    try:
        # SCRAM sends a one-time proof, never the password itself.
        with connect(**role_login):
            return []
    except psycopg.OperationalError:
        pass
    admin_login = connection_options(settings, "postgres", settings.admin_user, settings.admin_password)
    with connect(**admin_login, autocommit=True) as connection:
        connection.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(settings.role_name), sql.Literal(scram_password(settings.role_password))
            )
        )
    # Something other than the password can block the login, such as an
    # expired password or a server rule. Then the backend could not start either.
    try:
        with connect(**role_login):
            pass
    except psycopg.OperationalError:
        raise SetupError("the role still cannot log in after its password was set") from None
    return ["set the password of the role"]


def set_up(settings: SetupSettings, connect=psycopg.connect) -> list[str]:
    """Run each part of the step in order. Returns what changed."""
    changes = []
    admin = settings.admin_user, settings.admin_password
    with connect(**connection_options(settings, "postgres", *admin), autocommit=True) as connection:
        changes += ensure_role(connection, settings)
        changes += ensure_database(connection, settings)
        changes += block_kitaru_database(connection, settings)
    # PostgreSQL lists a database's tables only from inside that database.
    with connect(**connection_options(settings, settings.db_name, *admin), autocommit=True) as connection:
        changes += transfer_objects(connection, settings)
    changes += ensure_password(settings, connect)
    return changes


def run(env: Mapping[str, str] | None = None) -> int:
    """The setup-database command. The deployment starts the backend only on 0."""
    try:
        settings = SetupSettings.from_environment(env)
    except SettingsError as error:
        print(f"Evalve database setup settings: {error}", file=sys.stderr)
        return 2
    try:
        changes = set_up(settings)
    except psycopg.Error as error:
        # The error text can name the host, a user or a command. Print only its kind.
        # systemd runs the step again after a pause.
        print(f"The database setup failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    except SetupError as error:
        print(f"The database setup failed: {error}", file=sys.stderr)
        return 1
    print("Database setup: " + ("; ".join(changes) if changes else "no changes") + ".")
    return 0
