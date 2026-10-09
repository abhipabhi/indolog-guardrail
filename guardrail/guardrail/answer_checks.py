"""Checks on the model's reply, before anyone sees it. Each returns a note when it changes the reply."""
from __future__ import annotations

import re
from collections import Counter

from . import sources
from .grounding import source_for, unsupported_claims

_CODE = re.compile(r"```|^\s*(def|class)\s+\w+.*:\s*$|^\s*(import\s+\w+|from\s+\w[\w.]*\s+import\s)|"
                   r"\bfunction\s+\w+\s*\(|#include\s*<|console\.log\(|\bpublic\s+static\s+void\b|"
                   r"^\s*SELECT\s.+\sFROM\s", re.MULTILINE)
_MARKER = re.compile(r"\bDATA_[0-9a-f]{12}\b")


def no_code(text: str, rules) -> str | None:
    if _CODE.search(text):
        return "the model wrote code"
    return None


def no_leak(text: str, rules) -> str | None:
    """The fence marker, or any line of the instructions repeated word for word."""
    lines = [ln.replace("{marker}", "").strip(" -").lower()
             for ln in rules.input.system_instructions.splitlines() if len(ln.strip()) > 40]
    if _MARKER.search(text) or any(ln and ln in text.lower() for ln in lines):
        return "the reply repeated the model's instructions"
    return None


CHECKS = {"no_code": no_code, "no_leak": no_leak}   # "length" is applied last, separately


def grounding(text: str, rules, request_id: str) -> tuple[str, str] | None:
    """Are the dates, reference numbers and amounts in the reply stated in what the model was shown
    (the excerpts the input check saw for this request)? If one is not, the reply is withheld: a
    wrong ETA or invoice amount said with confidence is worse than no answer. Returns (reason,
    note) or None. The note counts what was unsupported; it never quotes it."""
    record = sources.lookup(request_id)
    if record is None:
        return "input_check_missing", "no input check is on record for this request, so the reply cannot be checked"
    missing = Counter(unsupported_claims(text, source_for(record.text())))
    if not missing:
        return None
    counted = ", ".join(f"{count} {kind}{'s' if count > 1 else ''}" for kind, count in sorted(missing.items()))
    return "grounding.unsupported", f"the reply states {counted} that the excerpts do not"


def refused_in_scope(request_id: str) -> bool:
    """Did the model refuse a question the input check had let through? That is not a leak or a
    harm, so nothing changes for the user; it is a sign the model is turning away real work."""
    record = sources.lookup(request_id)
    return record is not None and record.status in ("pass", "review")


# Checks that read the stored input for the request. They withhold the reply rather than refuse it.
SOURCE_CHECKS = {"grounding": grounding}
