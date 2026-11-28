"""Start the backend: check settings, prepare the database schema, then serve.

`python -m evalve_backend setup-database` runs the database setup step instead.
"""

import logging
import os
import sys
import time

import psycopg
import uvicorn

from evalve_backend import database, setup_database
from evalve_backend.app import create_app
from evalve_backend.migrations import migrate
from evalve_backend.settings import Settings, SettingsError

logger = logging.getLogger("evalve_backend")


def wait_for_database(settings: Settings, timeout: float, sleep=time.sleep, clock=time.monotonic) -> None:
    """Connect to the database, retrying while the server is still coming up."""
    deadline = clock() + timeout
    while True:
        try:
            database.check_connection(settings)
            return
        except psycopg.Error as error:
            if clock() >= deadline:
                raise
            logger.warning("Database is not ready yet (%s). Retrying.", type(error).__name__)
            sleep(3)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs each request line at INFO. Keep sign-in checks out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # botocore logs response bodies at DEBUG, and the body of an STS or ECR answer holds a credential.
    for name in ("boto3", "botocore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> int:
    configure_logging()
    if sys.argv[1:] == ["setup-database"]:
        return setup_database.run()
    try:
        settings = Settings.from_environment()
        timeout = float(os.environ.get("EVALVE_DB_WAIT_SECONDS", "120"))
    except (SettingsError, ValueError) as error:
        print(f"Evalve backend settings: {error}", file=sys.stderr)
        return 2
    try:
        wait_for_database(settings, timeout)
    except psycopg.Error as error:
        # The error text can name the host and user. Print only its kind.
        print(f"The database is not reachable ({type(error).__name__}).", file=sys.stderr)
        return 1
    try:
        applied = migrate(settings)
    except psycopg.Error as error:
        print(f"The database schema could not be migrated ({type(error).__name__}).", file=sys.stderr)
        return 1
    if applied:
        logger.info("Applied database schema versions: %s.", ", ".join(map(str, applied)))
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
