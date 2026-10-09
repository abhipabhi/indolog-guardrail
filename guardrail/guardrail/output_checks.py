"""Output checks: run on what the model extracted. Each returns its failures; empty means it passed.

To add one: write a function here, add it to CHECKS, and add its name to "output" -> "checks" in rules.json.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from . import sources
from .grounding import source_for, unsupported
from .leakage import kinds_in


def _missing(value: Any) -> bool:
    return value is None or value == [] or str(value).strip() == ""


# What each kind of schema error is called in a reviewer's words. The value itself is never quoted.
_SCHEMA_PROBLEMS = {
    "required": "is missing",
    "type": "is the wrong type",
    "enum": "is not one of the allowed values",
    "format": "is not in the expected format",
    "additionalProperties": "is not a field of this intent",
}


# When one value breaks two keywords at once (a null where an enum is wanted is both the wrong
# type and not an allowed value), the first of these is the one reported.
_SCHEMA_ORDER = ("required", "additionalProperties", "type", "enum", "format")


def _schema_leaves(error):
    """A nullable field is written as "this or null", so its error arrives wrapped in an anyOf.
    The useful errors are inside, on the branch that is not null."""
    inner = [c for c in (error.context or []) if not (c.validator == "type" and c.validator_value == "null")]
    if error.validator == "anyOf" and inner:
        for child in inner:
            yield from _schema_leaves(child)
    else:
        yield error


def schema(req, rules, config) -> list[dict]:
    """Does the extraction have the shape its intent's schema describes? The schema is the backend's
    own, exported into policy. An extraction that does not fit goes to a person; there is no repair,
    because the guardrail does not call the model. An intent with no schema in policy is not checked."""
    problems: dict[str, tuple[str, str]] = {}           # where -> (kind, top-level field)
    for wrapped in rules.schema_errors({name: field.value for name, field in req.fields.items()}):
        for error in _schema_leaves(wrapped):
            kind, path = error.validator, [str(part) for part in error.absolute_path]
            if kind == "required":
                names = [name for name in error.validator_value if name not in error.instance]
            elif kind == "additionalProperties":
                names = [name for name in error.instance if name not in error.schema.get("properties", {})]
            else:
                names = [None]
            for name in names:
                where = ".".join(path + ([name] if name else [])) or "the extraction"
                field = path[0] if path else name
                rank = _SCHEMA_ORDER.index(kind) if kind in _SCHEMA_ORDER else len(_SCHEMA_ORDER)
                known = problems.get(where)
                if known is None or rank < (_SCHEMA_ORDER.index(known[0]) if known[0] in _SCHEMA_ORDER else len(_SCHEMA_ORDER)):
                    problems[where] = (kind, field)
    return [{"field": field, "action": "review", "rule": f"schema.{kind}",
             "message": f"{where} {_SCHEMA_PROBLEMS.get(kind, 'does not fit the schema')}"}
            for where, (kind, field) in sorted(problems.items())]


def required_fields(req, rules, config) -> list[dict]:
    return [{"field": name, "message": f"{name} is required but missing"}
            for name in rules.required if _missing(req.value(name))]


def _allowed(value: Any, allowed: list[str]) -> bool:
    """The whole value, or its first word: Incoterms are written with a place, as in 'FOB Nhava Sheva'."""
    options = {a.casefold() for a in allowed}
    text = str(value).strip().casefold()
    first = re.split(r"[\s,;:()]+", text, maxsplit=1)[0]
    return text in options or first in options


def allowed_values(req, rules, config) -> list[dict]:
    failures = []
    for name, allowed in rules.allowed_values.items():
        value = req.value(name)
        if not _missing(value) and not _allowed(value, allowed):
            failures.append({"field": name, "message": f"{name} '{value}' is not one of: {', '.join(allowed)}"})
    return failures


_NUMBER = re.compile(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?")


def parse_number(value: Any) -> float | None:
    """First number in free text: '18,400 kg' -> 18400, '1,20,000' -> 120000, '18.4 MT' -> 18.4."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        match = _NUMBER.search(str(value))
        if not match:
            return None
        n = float(match.group(0).replace(",", ""))
    return n if math.isfinite(n) else None


def numbers(req, rules, config) -> list[dict]:
    failures = []
    for name in rules.numbers:
        value = req.value(name)
        if _missing(value):
            continue
        n = parse_number(value)
        if n is None or n <= 0:
            failures.append({"field": name, "message": f"{name} '{value}' doesn't contain a positive number"})
    return failures


def confidence(req, rules, config) -> list[dict]:
    failures = []
    for name, field in req.fields.items():
        if _missing(field.value):
            continue
        if rules.scored_fields is not None and name not in rules.scored_fields:
            continue        # the backend gives this field no score; nothing to hold it to
        needed = rules.min_confidence.get(name, config.min_confidence)
        if field.confidence is None:
            failures.append({"field": name, "message": f"{name} has no confidence score"})
        elif field.confidence < needed:
            failures.append({"field": name, "message": f"{name} confidence {field.confidence:.2f} is below {needed:.2f}"})
    scored_nothing = rules.scored_fields is not None and all(
        _missing(req.value(name)) for name in rules.scored_fields)
    if config.min_overall_confidence is not None and not scored_nothing:
        # An extraction that found nothing to score has no overall score, and needs none.
        overall = req.overall_confidence
        if overall is None:
            failures.append({"field": None, "message": "no overall confidence score"})
        elif overall < config.min_overall_confidence:
            failures.append({"field": None, "message": f"overall confidence {overall:.2f} is below "
                                                       f"{config.min_overall_confidence:.2f}"})
    # The scores themselves, so that a different minimum can be tried against recorded traffic.
    scored = [field.confidence for name, field in req.fields.items()
              if not _missing(field.value) and field.confidence is not None
              and (rules.scored_fields is None or name in rules.scored_fields)]
    measures = {"scored_fields": len(scored)}
    if scored:
        measures["lowest_field_confidence"] = min(scored)
    if req.overall_confidence is not None:
        measures["overall_confidence"] = req.overall_confidence
    return failures + [{"action": "allow", "measures": measures}]


def leakage(req, rules, config) -> list[dict]:
    """A secret, a card number or an Aadhaar number in an extracted value (guardrail/leakage.py). The
    backend takes only one edit from the guardrail, the removal of a value, so the value is removed
    rather than masked, and a person looks. The message names the field and the kind, never the value."""
    failures, counts = [], {}
    for name, field in req.fields.items():
        kinds = kinds_in(field.value)
        if not kinds:
            continue
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        what = ", ".join(sorted(set(kinds)))
        failures.append({"field": name, "action": "review", "rule": f"leakage.{kinds[0]}", "remove": True,
                         "message": f"{name} contains what looks like a {what}; the value was removed"})
    return failures + [{"action": "allow", "measures": {"found": sum(counts.values())}}]


def grounding(req, rules, config) -> list[dict]:
    """Is each extracted value stated in the text the input check saw? A value that is not is
    removed from what goes back and a person looks: the model made it up, or the email talked it
    into it. Fields the model is asked to infer or summarise are exempt in policy.

    No stored input for this request is a finding of its own (`input_check_missing`): the caller
    skipped the input check, or took longer than the record is kept."""
    record = sources.lookup(req.request_id)
    if record is None:
        return [{"field": None, "action": "review", "rule": "input_check_missing",
                 "message": "no input check is on record for this request, so the extraction cannot be held "
                            "against its source"}]
    if record.status in ("fail", "out_of_scope"):
        return [{"field": None, "action": "review", "rule": "grounding.input_not_passed",
                 "message": "the input check stopped this request, and it was extracted anyway"}]
    source = source_for(record.text(documents_only=True))
    exempt = set(rules.grounding_exempt)
    failures, checked = [], 0
    for name, field in req.fields.items():
        if name in exempt or _missing(field.value):
            continue
        checked += 1
        leaves = frozenset(entry.split(".", 1)[1] for entry in exempt if entry.startswith(f"{name}."))
        missing = unsupported(field.value, source, leaves)
        if missing:
            where = ", ".join(f"{name}.{path}" if path else name for path in missing[:5])
            failures.append({"field": name, "action": "review", "rule": "grounding.unsupported", "remove": True,
                             "message": f"{where} is not stated in the source text; the value was removed"})
    return failures + [{"action": "allow", "measures": {"fields_checked": checked, "fields_unsupported": len(failures)}}]


def high_impact_fields(req, rules, config) -> list[dict]:
    """A field that moves money or changes who receives the goods is seen by a person whenever it
    carries a value, until it can be checked against a record. Always a review, never a stop: the
    value is not wrong, it is important."""
    return [{"field": name, "action": "review", "rule": f"high_impact_fields.{name}",
             "message": f"{name} is a high-impact field and has a value, so a person should confirm it"}
            for name in rules.high_impact_fields if not _missing(req.value(name))]


def injection_flag(req, rules, config) -> list[dict]:
    """The model's instructions tell it to set possible_injection when the email tries to instruct it."""
    if req.possible_injection:
        return [{"field": None, "action": "review",
                 "message": "the model reported that the email tried to give it instructions"}]
    return []


CHECKS: dict[str, Callable] = {
    "schema": schema,
    "required_fields": required_fields,
    "allowed_values": allowed_values,
    "numbers": numbers,
    "confidence": confidence,
    "leakage": leakage,
    "grounding": grounding,
    "high_impact_fields": high_impact_fields,
    "injection_flag": injection_flag,
}
