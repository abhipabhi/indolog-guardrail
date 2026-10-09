"""Every decision as a row, and what a reviewer made of it.

The row holds what the decision log line holds: the request id, the stage, what the caller was
told, what it would have been told, the policy version, and each check's mode, verdict, score and
measures, with the id of every rule that fired in it. Numbers and ids. Never text.

Writing a decision must not slow an answer down or change it, so decisions go on a bounded queue
and a background thread writes them. If the database is slow or down, the queue fills and the
oldest waiting decisions are dropped, counted, and logged; the answers were given regardless, and
each decision is still in the service log.

RETENTION (open question Q45, decided by the owner on 3 Oct 2026). A decision row is kept for 365
days, or for GUARDRAIL_DECISION_RETENTION_DAYS if that is set, and never for fewer than 180: CERT-In
requires ICT-system logs to be kept for 180 days, and no setting may go under that. Rows older than
the period are removed once a day, with the feedback on them. Nothing else can delete a row (see
migration 0002). There are no partitions: at up to three rows per email and per chat question, a
daily DELETE on the `created_at` index is enough.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading

from .db import SCHEMA

log = logging.getLogger("guardrail")

DEFAULT_RETENTION_DAYS = 365
MINIMUM_RETENTION_DAYS = 180        # CERT-In's minimum for ICT-system logs; a floor, not a default


def retention_days() -> int:
    """How long a decision row is kept. A value that cannot be read falls back to the default, and
    one under the minimum is raised to the minimum: rows are never purged sooner than 180 days
    because of a typing mistake."""
    raw = os.environ.get("GUARDRAIL_DECISION_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        log.error("GUARDRAIL_DECISION_RETENTION_DAYS is not a whole number of days; keeping decisions for %s days",
                  DEFAULT_RETENTION_DAYS)
        return DEFAULT_RETENTION_DAYS
    if days < MINIMUM_RETENTION_DAYS:
        log.error("GUARDRAIL_DECISION_RETENTION_DAYS is %s, under the minimum of %s; keeping decisions for %s days",
                  days, MINIMUM_RETENTION_DAYS, MINIMUM_RETENTION_DAYS)
        return MINIMUM_RETENTION_DAYS
    return days


def purge(engine, days: int | None = None) -> tuple[int, int]:
    """Removes decision rows older than the retention period, and the feedback on them. Returns
    (decisions removed, feedback rows removed). `days` under the minimum is raised to it."""
    from sqlalchemy import text

    days = max(retention_days() if days is None else days, MINIMUM_RETENTION_DAYS)
    with engine.begin() as connection:
        # The tables accept a DELETE only from a transaction that says it is the purge (migration 0002).
        connection.execute(text("SELECT set_config('guardrail.retention_purge', 'on', true)"))
        cutoff = connection.execute(text("SELECT now() - make_interval(days => :days)"), {"days": days}).scalar()
        feedback = connection.execute(text(
            f"DELETE FROM {SCHEMA}.feedback f USING {SCHEMA}.decision d "
            "WHERE f.decision_id = d.id AND d.created_at < :cutoff"), {"cutoff": cutoff}).rowcount
        decisions = connection.execute(text(
            f"DELETE FROM {SCHEMA}.decision WHERE created_at < :cutoff"), {"cutoff": cutoff}).rowcount
    if decisions:
        log.info("purged %s decision row(s) and %s feedback row(s) older than %s days", decisions, feedback, days)
    return decisions, feedback


class DecisionWriter:
    """An engine observer. `engine.OBSERVERS.append(writer)` and every decision is queued for the table."""

    def __init__(self, engine, max_waiting: int = 10_000) -> None:
        self.engine = engine
        self.dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=max_waiting)
        self._thread = threading.Thread(target=self._write, name="guardrail-decision-writer", daemon=True)
        self._thread.start()

    def __call__(self, decision) -> None:
        row = {"request_id": decision.request_id, "stage": decision.stage, "status": decision.status,
               "would_be": decision.would_be, "version": decision.rules_version,
               "measures": json.dumps(decision.measures),
               "checks": json.dumps([{"id": o.filter_id, "mode": o.mode, "verdict": o.verdict, "reason": o.reason_code,
                                      "rules": o.rules, "score": o.score, "ms": round(o.latency_ms, 2),
                                      "error": o.error, "measures": o.measures} for o in decision.outcomes])}
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.error("the decision table is not keeping up: %s decision(s) not written (they are in the log)",
                          self.dropped)

    def _write(self) -> None:
        from sqlalchemy import text

        insert = text(f"INSERT INTO {SCHEMA}.decision (request_id, stage, status, would_be, policy_version, measures, checks) "
                      "VALUES (:request_id, :stage, :status, :would_be, :version, CAST(:measures AS JSONB), CAST(:checks AS JSONB))")
        while True:
            row = self._queue.get()
            try:
                if row is None:
                    return
                with self.engine.begin() as connection:
                    connection.execute(insert, row)
            except Exception as exc:        # noqa: BLE001 -- the answer was given; this is the record of it
                self.dropped += 1
                log.error("a decision could not be written to the table: %s", type(exc).__name__)
            finally:
                self._queue.task_done()

    def flush(self) -> None:
        """Waits until everything queued has been written (or has failed). For tests and shutdown."""
        self._queue.join()

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)


def record_feedback(engine, decision_id: int, label: str, reviewer: str) -> int | None:
    """A reviewer confirming or overturning one decision. Returns the feedback row's id, or None
    if there is no such decision."""
    from sqlalchemy import text

    with engine.begin() as connection:
        if connection.execute(text(f"SELECT 1 FROM {SCHEMA}.decision WHERE id = :id"), {"id": decision_id}).scalar() is None:
            return None
        return connection.execute(text(
            f"INSERT INTO {SCHEMA}.feedback (decision_id, label, reviewer) VALUES (:id, :label, :reviewer) RETURNING id"),
            {"id": decision_id, "label": label, "reviewer": reviewer}).scalar()


def decisions_for(engine, request_id: str) -> list[dict]:
    """The decisions recorded for one request, newest first, with any feedback on them."""
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(text(
            f"SELECT d.id, d.stage, d.status, d.would_be, d.policy_version, d.measures, d.checks, d.created_at, "
            f"       coalesce((SELECT json_agg(json_build_object('label', f.label, 'reviewer', f.reviewer, 'at', f.created_at) "
            f"                                 ORDER BY f.id) FROM {SCHEMA}.feedback f WHERE f.decision_id = d.id), '[]'::json) "
            f"FROM {SCHEMA}.decision d WHERE d.request_id = :id ORDER BY d.id DESC"), {"id": request_id}).all()
    return [{"id": r[0], "stage": r[1], "status": r[2], "would_be": r[3], "policy_version": r[4], "measures": r[5],
             "checks": r[6], "created_at": r[7].isoformat(), "feedback": r[8]} for r in rows]
