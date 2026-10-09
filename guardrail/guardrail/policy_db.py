"""The policy in Postgres: immutable versions, one active, every change audited.

A version is a whole policy document (the same shape as rules.json), validated by the same model
before it is stored, and never edited afterwards. Changing the policy means creating a new version
and activating it. Activating an older version is a rollback. Running services pick the active
version up without a restart: each one polls for it (`GUARDRAIL_POLICY_POLL_SECONDS`, 5 by default)
and swaps one reference when it changes.

`PostgresPolicyStore` makes the same promise as the file store: `get()` never returns an invalid
policy. If the database cannot be reached, or holds no active version, or the active version no
longer validates against this code, the last good policy stays in force and `error` says why. At
start, before anything has been read from the database, the last good policy is the file the
service was given (the bundled default).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from .db import SCHEMA, UNIQUE_VIOLATION, sqlstate
from .models import Rules
from .errors import NotReady
from .policy import PolicyStore

log = logging.getLogger("guardrail")


class PolicyError(ValueError):
    """A policy change that was refused. `code` is one of: invalid, duplicate, not_found,
    not_ready (valid, but an enforced check's model cannot be loaded where it is being activated)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Version:
    id: int
    label: str
    status: str
    note: str
    created_by: str
    created_at: datetime
    activated_by: str | None
    activated_at: datetime | None
    document: dict | None = None

    def summary(self) -> dict:
        return {"id": self.id, "label": self.label, "status": self.status, "note": self.note,
                "created_by": self.created_by, "created_at": self.created_at.isoformat(),
                "activated_by": self.activated_by,
                "activated_at": self.activated_at.isoformat() if self.activated_at else None}


_COLUMNS = "id, label, status, note, created_by, created_at, activated_by, activated_at"


def document_hash(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _version(row, with_document: bool = False) -> Version:
    return Version(*row[:8], document=row[8] if with_document else None)


class PolicyAdmin:
    """Creating, activating and reading policy versions. Used by the admin API and the command line."""

    def __init__(self, engine, ready=None) -> None:
        self.engine = engine
        self.ready = ready          # see activate()

    def create(self, document: dict, *, actor: str, note: str = "") -> Version:
        """Validates the document and stores it as a new draft version. The version's label is the
        document's own `version`. A refused change is audited too."""
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        try:
            rules = Rules.model_validate(document)
        except (ValidationError, ValueError) as exc:
            self._audit("rejected", None, actor, {"reason": "invalid", "errors": _error_count(exc)})
            raise PolicyError("invalid", str(exc)) from None
        try:
            with self.engine.begin() as connection:
                row = connection.execute(text(
                    f"INSERT INTO {SCHEMA}.policy_version (label, document, document_hash, note, created_by) "
                    f"VALUES (:label, CAST(:document AS JSONB), :hash, :note, :actor) RETURNING {_COLUMNS}"),
                    {"label": rules.version, "document": json.dumps(document, ensure_ascii=False),
                     "hash": document_hash(document), "note": note, "actor": actor}).one()
                connection.execute(text(
                    f"INSERT INTO {SCHEMA}.policy_audit (event, policy_version_id, actor, detail) "
                    "VALUES ('created', :id, :actor, CAST(:detail AS JSONB))"),
                    {"id": row[0], "actor": actor, "detail": json.dumps({"label": rules.version})})
        except DBAPIError as exc:
            if sqlstate(exc) != UNIQUE_VIOLATION:
                raise
            self._audit("rejected", None, actor, {"reason": "duplicate", "label": rules.version})
            raise PolicyError("duplicate", f"a version labelled {rules.version!r} already exists; "
                                           "give the document a new `version`") from None
        return _version(row)

    def activate(self, version_id: int, *, actor: str) -> Version:
        """Makes this version the active one and retires the one that was. One transaction, so
        there is never a moment with two active versions or none.

        With a `ready` hook (the service's is the classifier's `prepare`), a version whose enforced
        check cannot load its model here is refused before anything changes: the activation fails,
        the refusal is audited, and the version in force stays in force (open question Q53)."""
        from sqlalchemy import text

        if self.ready is not None:
            candidate = self.version(version_id)
            if candidate.status != "active":
                try:
                    self.ready(Rules.model_validate(candidate.document))
                except (ValidationError, ValueError):
                    pass            # refused below, with the reason, like any version that does not validate
                except Exception as exc:    # noqa: BLE001
                    detail = str(exc) if isinstance(exc, NotReady) else type(exc).__name__
                    self._audit("rejected", version_id, actor, {"reason": "not_ready", "error": type(exc).__name__})
                    raise PolicyError("not_ready", f"version {version_id} cannot be put in force: {detail}") from None

        with self.engine.begin() as connection:
            found = connection.execute(text(
                f"SELECT {_COLUMNS}, document FROM {SCHEMA}.policy_version WHERE id = :id FOR UPDATE"),
                {"id": version_id}).one_or_none()
            if found is None:
                raise PolicyError("not_found", f"there is no policy version {version_id}")
            try:
                Rules.model_validate(found[8])      # written by older code, perhaps: check it still loads
            except (ValidationError, ValueError) as exc:
                raise PolicyError("invalid", f"version {version_id} does not validate against this code: {exc}") from None
            if found[2] == "active":
                return _version(found)
            previous = connection.execute(text(
                f"UPDATE {SCHEMA}.policy_version SET status = 'retired' WHERE status = 'active' RETURNING id")).scalar()
            row = connection.execute(text(
                f"UPDATE {SCHEMA}.policy_version SET status = 'active', activated_by = :actor, activated_at = now() "
                f"WHERE id = :id RETURNING {_COLUMNS}"), {"id": version_id, "actor": actor}).one()
            if previous is not None:
                connection.execute(text(
                    f"INSERT INTO {SCHEMA}.policy_audit (event, policy_version_id, actor, detail) "
                    "VALUES ('retired', :id, :actor, CAST(:detail AS JSONB))"),
                    {"id": previous, "actor": actor, "detail": json.dumps({"replaced_by": version_id})})
            connection.execute(text(
                f"INSERT INTO {SCHEMA}.policy_audit (event, policy_version_id, actor, detail) "
                "VALUES ('activated', :id, :actor, CAST(:detail AS JSONB))"),
                {"id": version_id, "actor": actor, "detail": json.dumps({"replaced": previous})})
        return _version(row)

    def versions(self) -> list[Version]:
        from sqlalchemy import text
        with self.engine.connect() as connection:
            return [_version(row) for row in connection.execute(text(
                f"SELECT {_COLUMNS} FROM {SCHEMA}.policy_version ORDER BY id DESC"))]

    def version(self, version_id: int) -> Version:
        from sqlalchemy import text
        with self.engine.connect() as connection:
            row = connection.execute(text(
                f"SELECT {_COLUMNS}, document FROM {SCHEMA}.policy_version WHERE id = :id"), {"id": version_id}).one_or_none()
        if row is None:
            raise PolicyError("not_found", f"there is no policy version {version_id}")
        return _version(row, with_document=True)

    def active(self) -> Version | None:
        from sqlalchemy import text
        with self.engine.connect() as connection:
            row = connection.execute(text(
                f"SELECT {_COLUMNS}, document FROM {SCHEMA}.policy_version WHERE status = 'active'")).one_or_none()
        return _version(row, with_document=True) if row else None

    def audit(self, limit: int = 100) -> list[dict]:
        from sqlalchemy import text
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                f"SELECT id, event, policy_version_id, actor, detail, at FROM {SCHEMA}.policy_audit "
                "ORDER BY id DESC LIMIT :limit"), {"limit": limit}).all()
        return [{"id": r[0], "event": r[1], "policy_version_id": r[2], "actor": r[3], "detail": r[4],
                 "at": r[5].isoformat()} for r in rows]

    def _audit(self, event: str, version_id: int | None, actor: str, detail: dict) -> None:
        from sqlalchemy import text
        try:
            with self.engine.begin() as connection:
                connection.execute(text(
                    f"INSERT INTO {SCHEMA}.policy_audit (event, policy_version_id, actor, detail) "
                    "VALUES (:event, :id, :actor, CAST(:detail AS JSONB))"),
                    {"event": event, "id": version_id, "actor": actor, "detail": json.dumps(detail)})
        except Exception as exc:        # noqa: BLE001 -- the refusal itself still stands
            log.error("a refused policy change could not be audited: %s", type(exc).__name__)


def _error_count(exc: Exception) -> int:
    return len(exc.errors()) if isinstance(exc, ValidationError) else 1


class PostgresPolicyStore:
    """The active policy version, refreshed in the background. See the module docstring."""

    def __init__(self, engine, fallback: PolicyStore, poll_seconds: float | None = None, start: bool = True,
                 prepare=None) -> None:
        self.admin = PolicyAdmin(engine)
        self.fallback = fallback
        # Called with a new policy BEFORE any request sees it, on the thread that found it. The
        # service passes the classifier's `prepare`: the model is loaded here, not inside the
        # first request that needs it, and a policy whose ENFORCED check cannot load its model is
        # not swapped in at all.
        self.prepare = prepare
        self.poll_seconds = poll_seconds if poll_seconds is not None else float(
            os.environ.get("GUARDRAIL_POLICY_POLL_SECONDS", "5"))
        self.error: str | None = None
        self._rules: Rules | None = None
        self._loaded: tuple[int, str] | None = None        # (version id, document hash) in force
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.refresh()
        if start:
            threading.Thread(target=self._poll, name="guardrail-policy-poll", daemon=True).start()

    def get(self) -> Rules:
        rules = self._rules
        return rules if rules is not None else self.fallback.get()

    def describe(self) -> str:
        loaded = self._loaded
        if loaded is None:
            return f"file ({self.fallback.describe()}); no active version read from the database"
        return f"database, policy version id {loaded[0]}"

    def refresh(self) -> bool:
        """Reads the active version and swaps it in if it changed. True if the policy in force
        came from the database after this call. Never raises."""
        try:
            active = self.admin.active()
        except Exception as exc:        # noqa: BLE001 -- the database is down or unreachable
            self._keep(f"the policy database could not be read ({type(exc).__name__}); the last good policy stays in force")
            return self._loaded is not None
        if active is None:
            self._keep("the policy database holds no active version; the file policy is in force")
            return self._loaded is not None
        signature = (active.id, document_hash(active.document))
        if signature == self._loaded:
            self.error = None
            return True
        try:
            rules = Rules.model_validate(active.document)
        except (ValidationError, ValueError) as exc:
            self._keep(f"active policy version {active.id} does not validate against this code "
                       f"({_error_count(exc)} error(s)); the last good policy stays in force")
            return self._loaded is not None
        if self.prepare is not None:
            # The hook raises when the policy cannot be put in force HERE: an enforced check's model
            # will not load (open question Q53). Then this replica keeps its last good policy and
            # says so on /health, exactly as for a version that does not validate. A shadow check's
            # model failing to load is not such a case; the hook does not raise for it.
            try:
                self.prepare(rules)
            except Exception as exc:    # noqa: BLE001
                detail = str(exc) if isinstance(exc, NotReady) else type(exc).__name__
                self._keep(f"active policy version {active.id} cannot be put in force on this service ({detail}); "
                           "the last good policy stays in force")
                return self._loaded is not None
        with self._lock:
            self._rules, self._loaded, self.error = rules, signature, None
        log.info("policy version %s (id %s) loaded from the database", rules.version, active.id)
        return True

    def _keep(self, reason: str) -> None:
        if self.error != reason:
            log.warning(reason)
        self.error = reason

    def _poll(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.refresh()

    def close(self) -> None:
        self._stop.set()
