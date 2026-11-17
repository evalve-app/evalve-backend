"""The backend's own PostgreSQL database.

It lives on the same PostgreSQL server as Kitaru's database, but it is a
separate database, so Kitaru's migrations never touch it.
"""

import logging
import selectors
from contextlib import closing
from time import monotonic

import psycopg
from psycopg import pq, sql

from evalve_backend.settings import Settings

logger = logging.getLogger(__name__)

MAINTENANCE_DATABASE = "postgres"
HEALTH_QUERY_TIMEOUT = 5.0


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
        # Close the socket directly, even when PostgreSQL stops responding mid-query.
        with closing(connect(**connection_options(settings, settings.db_name))) as connection:
            return _query_answers(connection.pgconn)
    except (psycopg.Error, OSError) as error:
        # The error text can name the host and user. Log only its kind.
        logger.warning("Database check failed (%s).", type(error).__name__)
        return False


def _query_answers(pgconn: pq.PGconn) -> bool:
    deadline = monotonic() + HEALTH_QUERY_TIMEOUT
    pgconn.nonblocking = 1
    pgconn.send_query(b"SELECT 1")
    pending_write = pgconn.flush()

    with selectors.DefaultSelector() as selector:
        events = selectors.EVENT_READ | selectors.EVENT_WRITE if pending_write else selectors.EVENT_READ
        selector.register(pgconn.socket, events)
        while pending_write or pgconn.is_busy():
            remaining = deadline - monotonic()
            if remaining <= 0 or not (ready := selector.select(remaining)):
                raise TimeoutError("Database health query exceeded its deadline.")
            _, events = ready[0]
            if events & selectors.EVENT_READ:
                pgconn.consume_input()
            if pending_write:
                pending_write = pgconn.flush()
                if not pending_write:
                    selector.modify(pgconn.socket, selectors.EVENT_READ)

    result = pgconn.get_result()
    return (
        result is not None and result.status == pq.ExecStatus.TUPLES_OK
        and result.ntuples == 1 and result.get_value(0, 0) == b"1"
    )
