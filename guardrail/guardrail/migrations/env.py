"""Alembic environment for the guardrail's own tables.

Separate from the backend's Alembic environment on purpose: its own folder (inside the package, so
it ships with it), its own Postgres schema (`guardrail`) and its own version table inside that
schema, so neither chain sees the other's tables. Run through
`python -m guardrail.policy_admin migrate`; the engine comes from guardrail.db, so the database is
read from the environment exactly as the service reads it, and nothing here holds a credential."""
from alembic import context
from sqlalchemy import text

from guardrail.db import SCHEMA, engine_from_env

config = context.config


def run_migrations_online() -> None:
    engine = config.attributes.get("engine") or engine_from_env()
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
        context.configure(connection=connection, target_metadata=None, version_table="alembic_version",
                          version_table_schema=SCHEMA)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported; run against a database")
run_migrations_online()
