"""The backend's own PostgreSQL database.

It lives on the same PostgreSQL server as Kitaru's database, but it is a
separate database, so Kitaru's migrations never touch it.
"""

import logging

import psycopg
from psycopg import sql

from evalve_backend.settings import Settings

logger = logging.getLogger(__name__)

MAINTENANCE_DATABASE = "postgres"


def connection_options(settings: Settings, database: str) -> dict:
    options = {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_password,
        "dbname": database,
        "sslmode": settings.db_ssl_mode,
        "connect_timeout": 5,
        "application_name": "evalve-backend",
    }
    if settings.db_ssl_ca:
        options["sslrootcert"] = settings.db_ssl_ca
    return options


def ensure_database(settings: Settings, connect=psycopg.connect) -> bool:
    """Create the backend's database if the server does not have it yet.

    Returns True when this call created it. A database restored from a snapshot
    already exists, so this leaves it alone.
    """
    with connect(**connection_options(settings, MAINTENANCE_DATABASE), autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.db_name,)
        ).fetchone()
        if exists:
            return False
        try:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.db_name)))
        except psycopg.errors.DuplicateDatabase:
            # Another process created it between the check and the create.
            return False
        return True


def ping(settings: Settings, connect=psycopg.connect) -> bool:
    """Report whether the backend's database answers a query."""
    try:
        with connect(**connection_options(settings, settings.db_name)) as connection:
            return connection.execute("SELECT 1").fetchone() == (1,)
    except psycopg.Error as error:
        # The error text can name the host and user. Log only its kind.
        logger.warning("Database check failed (%s).", type(error).__name__)
        return False
