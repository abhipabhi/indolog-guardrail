"""What the input check saw, kept so that later checks can hold the model's output against it.

`/guardrail/output` and `/guardrail/answer` are not sent the source text, and the contract is
frozen, so `/guardrail/input` stores it under the request id and the later call for the same id
reads it back. A later call with nothing stored is itself a finding (`input_check_missing`): it
means the caller skipped the input check, or the record expired.

  * The text stored is the normalized text the checks read. It is NEVER logged, and a record's
    repr shows sizes only.
  * Write-once per request id (open question Q1, answered 3 Oct). A retry with the same text is
    accepted; different text under an id already on record is refused and the input check comes
    back review with the reason `source_conflict`. The first record stands until it expires.
  * Records expire (`sources.ttl_seconds` in policy) and are purged.
  * `SourceStore` is the interface. `MemorySourceStore` keeps records in this process, which is
    right for one replica and for tests, and WRONG for a deployment with several replicas or with
    the input and output checks made by different processes: there the Postgres store is needed.
"""
from __future__ import annotations

import contextvars
import hashlib
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class InputRecord:
    request_id: str
    parts: tuple[tuple[str, str], ...]          # (question | body | attachment name, normalized text)
    status: str                                 # what the input check answered: pass, review, fail, out_of_scope
    reasons: tuple[str, ...] = ()               # the rules that fired, enforced or shadow
    rules_version: str = ""
    stored_at: float = field(default_factory=time.time)

    @property
    def content_hash(self) -> str:
        """What makes two records the same request: the parts and their text."""
        digest = hashlib.sha256()
        for name, text in self.parts:
            digest.update(name.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
        return digest.hexdigest()

    def text(self, *, documents_only: bool = False) -> str:
        return "\n".join(text for name, text in self.parts if not (documents_only and name == "question"))

    def __repr__(self) -> str:                  # never the text
        return (f"InputRecord({self.request_id!r}, status={self.status!r}, parts={len(self.parts)}, "
                f"chars={sum(len(text) for _, text in self.parts)})")


STORED, SAME, CONFLICT = "stored", "same", "conflict"


class SourceStore(Protocol):
    def put(self, record: InputRecord, ttl_seconds: int) -> str:
        """Keep this record until it expires, unless one is already on record for its request id.
        Returns STORED, SAME (the record on file has the same text; nothing changes) or CONFLICT
        (it has different text; nothing changes)."""

    def get(self, request_id: str) -> InputRecord | None:
        """The record for this request, or None if there is none or it has expired."""

    def purge(self) -> int:
        """Remove expired records. Returns how many were removed."""


class MemorySourceStore:
    """Records in this process's memory. Bounded twice over, by count and by the total text held:
    when either is exceeded the oldest records go. An email can be a quarter of a million
    characters, so a count alone would not bound the memory."""

    def __init__(self, max_records: int = 10_000, max_total_chars: int = 50_000_000) -> None:
        self.max_records, self.max_total_chars = max_records, max_total_chars
        self._records: OrderedDict[str, tuple[float, InputRecord]] = OrderedDict()
        self._chars = 0
        self._lock = threading.Lock()

    @staticmethod
    def _size(record: InputRecord) -> int:
        return sum(len(text) for _, text in record.parts)

    def _drop(self, request_id: str) -> None:
        held = self._records.pop(request_id, None)
        if held is not None:
            self._chars -= self._size(held[1])

    def put(self, record: InputRecord, ttl_seconds: int) -> str:
        with self._lock:
            held = self._records.get(record.request_id)
            if held is not None and held[0] > time.time():
                return SAME if held[1].content_hash == record.content_hash else CONFLICT
            self._drop(record.request_id)
            self._records[record.request_id] = (time.time() + ttl_seconds, record)
            self._chars += self._size(record)
            while len(self._records) > 1 and (len(self._records) > self.max_records
                                               or self._chars > self.max_total_chars):
                self._drop(next(iter(self._records)))
            return STORED

    def get(self, request_id: str) -> InputRecord | None:
        with self._lock:
            held = self._records.get(request_id)
            if held is None:
                return None
            if held[0] <= time.time():
                self._drop(request_id)
                return None
            return held[1]

    def purge(self) -> int:
        with self._lock:
            now = time.time()
            expired = [request_id for request_id, (expires, _) in self._records.items() if expires <= now]
            for request_id in expired:
                self._drop(request_id)
            return len(expired)

    def __len__(self) -> int:
        return len(self._records)


# --- the store in force for the request being checked -------------------------------------------------

_store: contextvars.ContextVar[SourceStore | None] = contextvars.ContextVar("guardrail_sources", default=None)


@contextmanager
def using(store: SourceStore | None):
    token = _store.set(store)
    try:
        yield
    finally:
        _store.reset(token)


class NoStore(RuntimeError):
    """A check needed the stored input and the service has no store configured."""


def lookup(request_id: str) -> InputRecord | None:
    """The stored input for this request. Raises NoStore (so the engine fails closed) when the
    service was started without a store; returns None when the store has no record."""
    store = _store.get()
    if store is None:
        raise NoStore("no source store is configured")
    return store.get(request_id)
