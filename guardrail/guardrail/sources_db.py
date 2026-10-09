"""What each input check saw, in Postgres, so that every replica and both consumers see the same record.

The same contract as the in-memory store in sources.py: write-once per request id, a retry with the
same text accepted, different text refused, records gone once they expire. The text is the
normalized text the checks read; it is never logged, and nothing here logs a row.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .db import SCHEMA
from .sources import CONFLICT, SAME, STORED, InputRecord

log = logging.getLogger("guardrail")


class PostgresSourceStore:
    def __init__(self, engine) -> None:
        self.engine = engine

    def put(self, record: InputRecord, ttl_seconds: int) -> str:
        from sqlalchemy import text

        values = {"id": record.request_id, "hash": record.content_hash,
                  "parts": json.dumps([list(part) for part in record.parts], ensure_ascii=False),
                  "status": record.status, "reasons": json.dumps(list(record.reasons)),
                  "version": record.rules_version, "ttl": ttl_seconds}
        with self.engine.begin() as connection:
            # One statement decides it: insert if the id is free, or take over a row that has
            # expired. A live row is never touched, so two replicas racing cannot both win.
            stored = connection.execute(text(
                f"INSERT INTO {SCHEMA}.request_context AS held "
                "(request_id, content_hash, parts, status, reasons, rules_version, expires_at) "
                "VALUES (:id, :hash, CAST(:parts AS JSONB), :status, CAST(:reasons AS JSONB), :version, "
                "        now() + make_interval(secs => :ttl)) "
                "ON CONFLICT (request_id) DO UPDATE SET content_hash = EXCLUDED.content_hash, parts = EXCLUDED.parts, "
                "    status = EXCLUDED.status, reasons = EXCLUDED.reasons, rules_version = EXCLUDED.rules_version, "
                "    stored_at = now(), expires_at = EXCLUDED.expires_at "
                "WHERE held.expires_at <= now() RETURNING request_id"), values).one_or_none()
            if stored is not None:
                return STORED
            held = connection.execute(text(
                f"SELECT content_hash FROM {SCHEMA}.request_context WHERE request_id = :id"), {"id": record.request_id}).scalar()
        return SAME if held == record.content_hash else CONFLICT

    def get(self, request_id: str) -> InputRecord | None:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            row = connection.execute(text(
                f"SELECT parts, status, reasons, rules_version, stored_at FROM {SCHEMA}.request_context "
                "WHERE request_id = :id AND expires_at > now()"), {"id": request_id}).one_or_none()
        if row is None:
            return None
        stored_at: datetime = row[4]
        return InputRecord(request_id=request_id, parts=tuple((name, text_) for name, text_ in row[0]),
                           status=row[1], reasons=tuple(row[2]), rules_version=row[3],
                           stored_at=stored_at.replace(tzinfo=stored_at.tzinfo or timezone.utc).timestamp())

    def purge(self) -> int:
        from sqlalchemy import text

        with self.engine.begin() as connection:
            removed = connection.execute(text(f"DELETE FROM {SCHEMA}.request_context WHERE expires_at <= now()")).rowcount
        if removed:
            log.info("purged %s expired input record(s)", removed)
        return removed
