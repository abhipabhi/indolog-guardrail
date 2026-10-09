"""Where the policy comes from.

Every filter reads its settings from a `Rules` object, and gets that object only through a
`PolicyStore`. The store decides where the policy lives; filters never know. Two backends:

    JsonPolicyStore       rules.json on disk. Today's behaviour, the bundled default, and the
                          last-known-good that a database-backed store falls back to.
    PostgresPolicyStore   versioned, audited policy in Postgres (guardrail/policy_db.py).

Both make the same promise: `get()` returns a policy that passed validation, a change takes effect
without a restart, and a change that fails validation leaves the previous policy in force with the
reason in `error`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .models import Rules

log = logging.getLogger("guardrail")

# rules.json beside the package: the bundled default.
BUNDLED_RULES = Path(__file__).resolve().parents[1] / "rules.json"


class PolicyStore(Protocol):
    """What the engine needs from wherever the policy lives."""

    error: str | None

    def get(self) -> Rules:
        """The policy in force. Never an invalid one."""

    def describe(self) -> str:
        """Where this policy came from, for /rules and /health. No secrets."""


class JsonPolicyStore:
    """Reads rules.json and re-reads it whenever the file changes, so a rule change needs no restart.
    If an edit breaks the file, the last good rules stay in force and the error is reported."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.error: str | None = None
        self._rules: Rules | None = None
        self._mtime: int | None = None
        self._lock = threading.Lock()

    def get(self) -> Rules:
        with self._lock:
            mtime = self.path.stat().st_mtime_ns
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._rules = Rules.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
                    self.error = None
                    log.info("rules version %s loaded from %s", self._rules.version, self.path)
                except (ValueError, ValidationError) as exc:
                    self.error = str(exc)
                    if self._rules is None:
                        raise
                    log.warning("rules.json is invalid, keeping version %s: %s", self._rules.version, self.error)
            return self._rules

    def describe(self) -> str:
        return str(self.path)


def store_from_env() -> PolicyStore:
    """The store the service uses: rules.json at GUARDRAIL_RULES, or the bundled one."""
    return JsonPolicyStore(os.environ.get("GUARDRAIL_RULES", BUNDLED_RULES))
