"""Versioned migrations for Evalve's own PostgreSQL database."""

import psycopg

from evalve_backend.database import connection_options
from evalve_backend.settings import Settings

# The two-integer advisory lock namespace is separate from one-bigint Loop locks.
_MIGRATION_LOCK = (0x4556414C, 0x56454442)

MIGRATIONS = (
    (1, """
        CREATE TABLE loops (
            id text PRIMARY KEY,
            owner_account_id text NOT NULL,
            name text NOT NULL,
            slug text NOT NULL,
            agent_id text NOT NULL,
            status text NOT NULL DEFAULT 'running',
            outcome text,
            reason text,
            config jsonb NOT NULL,
            preflight jsonb NOT NULL,
            pending_change jsonb,
            waiting_since timestamptz,
            idempotency_key text NOT NULL,
            request_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (owner_account_id, idempotency_key),
            CHECK (
                (status = 'running' AND outcome IS NULL)
                OR (outcome IS NOT NULL AND (
                    (status = 'completed' AND outcome IN (
                        'passed', 'already_passing', 'max_runs', 'budget_reached', 'stopped_idle'
                    ))
                    OR (status = 'cancelled' AND outcome = 'stopped_by_user')
                    OR (status = 'failed' AND outcome = 'error')
                ))
            )
        );
        CREATE UNIQUE INDEX loops_active_agent_slug ON loops (agent_id, slug)
            WHERE status = 'running';
        CREATE INDEX loops_owner_created ON loops (owner_account_id, created_at DESC, id DESC);

        CREATE TABLE experiments (
            loop_id text NOT NULL REFERENCES loops (id),
            number integer NOT NULL CHECK (number >= 0),
            name text NOT NULL,
            override jsonb NOT NULL,
            change text NOT NULL,
            experiment_id text,
            PRIMARY KEY (loop_id, number)
        );

        CREATE TABLE runs (
            loop_id text NOT NULL,
            experiment_number integer NOT NULL,
            number integer NOT NULL CHECK (number > 0),
            estimated_cost_usd numeric NOT NULL CHECK (
                estimated_cost_usd >= 0 AND estimated_cost_usd < 'Infinity'::numeric
            ),
            cohort_version_id text NOT NULL,
            agent_version_id text NOT NULL,
            submit_started_at timestamptz NOT NULL,
            run_id text CONSTRAINT runs_kitaru_run_id_key UNIQUE,
            result jsonb,
            evidence jsonb,
            collected_at timestamptz,
            PRIMARY KEY (loop_id, experiment_number, number),
            FOREIGN KEY (loop_id, experiment_number) REFERENCES experiments (loop_id, number),
            CHECK (
                (result IS NULL AND evidence IS NULL AND collected_at IS NULL)
                OR (run_id IS NOT NULL AND result IS NOT NULL AND evidence IS NOT NULL AND collected_at IS NOT NULL)
            )
        );
    """),
)


def migrate(settings: Settings) -> tuple[int, ...]:
    """Apply missing versions and record them in the same transaction as the DDL."""
    with psycopg.connect(**connection_options(settings, settings.db_name)) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s::integer, %s::integer)", _MIGRATION_LOCK)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version integer PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        existing = {version for (version,) in connection.execute("SELECT version FROM schema_migrations")}
        applied = []
        for version, statements in MIGRATIONS:
            if version in existing:
                continue
            connection.execute(statements)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
        return tuple(applied)
