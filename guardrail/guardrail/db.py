"""The guardrail's database connection.

Optional. With no database configured the service runs exactly as before: policy from rules.json,
the input store in memory, decisions in the log only. With one, the policy is versioned and
audited, what each input check saw is shared by every replica, and every decision is a row.

Configured from the environment, either way:

    GUARDRAIL_DATABASE_URL       postgresql://user:password@host:5432/dbname
or  GUARDRAIL_DATABASE_HOST, _PORT (5432), _NAME, _USER, _PASSWORD
    (separate parts, as the backend does, so a password needs no URL-escaping)

    GUARDRAIL_DATABASE_SSL       require (the default; Azure Flexible Server needs it) | disable

Everything lives in the Postgres schema `guardrail`, in the same database as the backend or any
other; nothing outside that schema is read or written. SQLAlchemy and pg8000 are imported only when
a database is configured.
"""
from __future__ import annotations

import os
import ssl

SCHEMA = "guardrail"


def configured() -> bool:
    return bool(os.environ.get("GUARDRAIL_DATABASE_URL") or os.environ.get("GUARDRAIL_DATABASE_HOST"))


def url_from_env():
    """The SQLAlchemy URL, always on the pg8000 driver whatever scheme the URL was written with.
    Its repr hides the password."""
    from sqlalchemy.engine import URL, make_url

    raw = os.environ.get("GUARDRAIL_DATABASE_URL")
    if raw:
        return make_url(raw).set(drivername="postgresql+pg8000", query={})
    host = os.environ.get("GUARDRAIL_DATABASE_HOST")
    if not host:
        raise RuntimeError("no database is configured (GUARDRAIL_DATABASE_URL or GUARDRAIL_DATABASE_HOST)")
    return URL.create("postgresql+pg8000", username=os.environ.get("GUARDRAIL_DATABASE_USER"),
                      password=os.environ.get("GUARDRAIL_DATABASE_PASSWORD"), host=host,
                      port=int(os.environ.get("GUARDRAIL_DATABASE_PORT", "5432")),
                      database=os.environ.get("GUARDRAIL_DATABASE_NAME"))


def make_engine(url, *, ssl_mode: str | None = None, timeout_seconds: int = 5):
    """An engine with a small pool. Connections are checked before use, so a database restart costs
    one reconnect, not an error; a connection attempt gives up after `timeout_seconds`."""
    from sqlalchemy import create_engine

    mode = (ssl_mode or os.environ.get("GUARDRAIL_DATABASE_SSL", "require")).lower()
    if mode not in ("require", "disable"):
        raise ValueError("GUARDRAIL_DATABASE_SSL must be 'require' or 'disable'")
    connect_args: dict = {"timeout": timeout_seconds}
    if mode == "require":
        connect_args["ssl_context"] = ssl.create_default_context()
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800,
                         pool_timeout=timeout_seconds, connect_args=connect_args)


def engine_from_env():
    return make_engine(url_from_env())


UNIQUE_VIOLATION, FOREIGN_KEY_VIOLATION = "23505", "23503"


def sqlstate(error) -> str | None:
    """The Postgres error code behind a SQLAlchemy DBAPIError. The driver does not sort every
    constraint violation into the same exception class, so the code is what is checked."""
    original = getattr(error, "orig", None)
    detail = original.args[0] if getattr(original, "args", None) else None
    return detail.get("C") if isinstance(detail, dict) else None
