"""Is a value the model returned actually stated in the text it was given?

Deliberately simple and explainable. A value is supported when:

  text    its words appear in the source as a phrase, or all of them close together
          (so "FOB Nhava Sheva" is supported by "Nhava Sheva, FOB");
  number  the same number appears in the source, however it is grouped (18,400 / 1,24,500 / 18400.0);
  date    the same day appears in the source in any common written form (5 Oct 2026, 05/10/2026,
          October 5th, 2026-10-05). A written date without a year matches any year.

Nothing is inferred: "18.4 MT" does not support "18,400 kg", and "next Friday" supports no date.
Fields the model is asked to infer or summarise are exempted in policy, not here.

What is folded away is only what cannot change a value (open question Q34, answered 3 Oct): case,
whitespace, digit grouping, the dots in a code written F.O.B., and the spelling of a unit ("kgs" is
"kg", "m3" is "cbm", "ctns" is "carton"). A conversion between units, or a year worked out for a
date, is not folded; those wait for real pairs of email and extraction.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from functools import lru_cache
from typing import Any, Iterator

from .normalize import strip_hidden

_GROUPED = re.compile(r"(?<=\d),(?=\d)")                       # 18,400 and 1,24,500 -> 18400, 124500
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[^\W\d_]+", re.UNICODE)    # a number, or a run of letters
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = {name: number for number, names in enumerate(
    ("jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july", "aug august",
     "sep sept september", "oct october", "nov november", "dec december"), 1) for name in names.split()}
_MONTH = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?"
_DATES = (
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})\b"), ("a", "b", "y")),          # day and month either way round
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]*(?:of\s+)?" + _MONTH + r",?[\s\-]*(\d{4}|\d{2})?\b"), ("d", "M", "y")),
    (re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b,?\s*(\d{4})?"), ("M", "d", "y")),
)


# Letters written with dots between them: F.O.B., C.I.F, U.S.A.
_DOTTED = re.compile(r"(?<![\w.])((?:[^\W\d_]\.){2,}[^\W\d_]?)\.?(?![\w])")
# Units written as a number-like token, or in two words, that mean the same as a one-word spelling.
_UNIT_PHRASES = re.compile(r"(?<![\w])(?:m\s?3|cu\.?\s?m(?:tr|etres?|eters?)?|cubic\s+met(?:re|er)s?)(?![\w])")
# Spellings of the same unit, and of the same kind of package. Plural, abbreviation, full word.
_SPELLINGS = {
    **dict.fromkeys(("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "kilogramme", "kilogrammes"), "kg"),
    **dict.fromkeys(("cbm", "cbms"), "cbm"),
    **dict.fromkeys(("mt", "mts", "tonne", "tonnes"), "tonne"),
    **dict.fromkeys(("lb", "lbs"), "lb"),
    **dict.fromkeys(("ctn", "ctns", "carton", "cartons"), "carton"),
    **dict.fromkeys(("pc", "pcs", "piece", "pieces"), "piece"),
    **dict.fromkeys(("plt", "plts", "pallet", "pallets"), "pallet"),
    **dict.fromkeys(("pkg", "pkgs", "package", "packages"), "package"),
}


def fold(text: str) -> str:
    """The text as it is compared: hidden characters out, compatibility forms folded, lower case,
    digit grouping removed, dotted codes closed up, two-word unit spellings made one word."""
    folded = _GROUPED.sub("", unicodedata.normalize("NFKC", strip_hidden(text)).casefold())
    folded = _DOTTED.sub(lambda m: m.group(1).replace(".", ""), folded)
    return _UNIT_PHRASES.sub(" cbm ", folded)


def _canonical(found: list[str]) -> list[str]:
    return [_SPELLINGS.get(token, token) for token in found]


def tokens(text: str) -> list[str]:
    return _canonical(_TOKEN.findall(fold(text)))


class Source:
    """The source text, indexed once so that many values can be checked against it."""

    def __init__(self, text: str) -> None:
        folded = fold(text)
        self.tokens = _canonical(_TOKEN.findall(folded))
        self.positions: dict[str, list[int]] = {}
        for index, token in enumerate(self.tokens):
            self.positions.setdefault(token, []).append(index)
        self.numbers = {float(n) for n in _NUMBER.findall(folded)}
        self.dates = set(_dates(folded))

    def has_text(self, value: str) -> bool:
        wanted = tokens(value)
        if not wanted:
            return True                                   # punctuation only: nothing to hold it to
        if any(token not in self.positions for token in wanted):
            return False
        first = self.positions[wanted[0]]
        if any(self.tokens[start:start + len(wanted)] == wanted for start in first):
            return True                                   # the phrase itself
        # All of its words, close together, in any order: the shortest stretch of the source that
        # holds every one of them is not much longer than the value itself.
        return _shortest_span([self.positions[token] for token in sorted(set(wanted))]) <= 2 * len(wanted) + 4

    def has_number(self, value: float) -> bool:
        return any(abs(value - seen) <= 1e-6 * max(1.0, abs(value)) for seen in self.numbers)

    def has_date(self, value: date) -> bool:
        return any(month == value.month and day == value.day and year in (None, value.year)
                   for year, month, day in self.dates)


def _shortest_span(lists: list[list[int]]) -> int:
    """The length of the shortest stretch that contains one position from every list."""
    merged = sorted((position, which) for which, positions in enumerate(lists) for position in positions)
    held, missing, best, left = [0] * len(lists), len(lists), len(merged) and merged[-1][0] + 1, 0
    for position, which in merged:
        held[which] += 1
        missing -= held[which] == 1
        while not missing:
            best = min(best, position - merged[left][0])
            held[merged[left][1]] -= 1
            missing += held[merged[left][1]] == 0
            left += 1
    return best


def _dates(folded: str) -> Iterator[tuple[int | None, int, int]]:
    """Every date written in the text, as (year or None, month, day)."""
    for pattern, order in _DATES:
        for match in pattern.finditer(folded):
            parts = dict(zip(order, match.groups()))
            year = int(parts["y"]) if parts.get("y") else None
            if year is not None and year < 100:
                year += 2000
            if "M" in parts:
                candidates = [(_MONTHS[parts["M"]], int(parts["d"]))]
            elif "a" in parts:                            # 05/10/2026: 5 October, or 10 May
                a, b = int(parts["a"]), int(parts["b"])
                candidates = [(b, a), (a, b)]
            else:
                candidates = [(int(parts["m"]), int(parts["d"]))]
            for month, day in candidates:
                if 1 <= month <= 12 and 1 <= day <= 31:
                    yield year, month, day


# --- claims in free text (a chat reply) ---------------------------------------------------------------

# A reference: letters and digits together, six characters or more (MAEU123456789, FF-20418, CI-4821).
_REFERENCE = re.compile(r"(?<![\w\-/])(?=[\w\-/]*\d)(?=[\w\-/]*[^\W\d_])[^\W_][\w\-/]{4,}[^\W_](?![\w\-/])")
# An amount: a number of four digits or more, or one with decimals. Small whole numbers are counts
# and durations a reply may fairly work out for itself.
_AMOUNT = re.compile(r"(?<![\w.])(?:\d{4,}|\d+\.\d+)(?!\w|\.\d)")


def unsupported_claims(reply: str, source: Source) -> list[str]:
    """The checkable facts in a reply that the source does not state, by kind: "date",
    "reference", "amount". Names, places and prose are not checked; nothing here can tell whether
    they are right. Never returns the fact itself."""
    folded, missing = fold(reply), []
    spans: list[tuple[int, int]] = []
    for pattern, _ in _DATES:
        for match in pattern.finditer(folded):
            if any(start <= match.start() < end for start, end in spans):
                continue
            written = list(_dates(match.group(0)))
            if not written:
                continue
            spans.append(match.span())
            if not any(month == m and day == d and (year is None or y is None or year == y)
                       for year, month, day in written for y, m, d in source.dates):
                missing.append("date")
    rest = _blank(folded, spans)
    spans = []
    for match in _REFERENCE.finditer(rest):
        spans.append(match.span())
        if not source.has_text(match.group(0)):
            missing.append("reference")
    for match in _AMOUNT.finditer(_blank(rest, spans)):
        if not source.has_number(float(match.group(0))):
            missing.append("amount")
    return missing


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in spans:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


@lru_cache(maxsize=32)
def source_for(text: str) -> Source:
    return Source(text)


def unsupported(value: Any, source: Source, exempt: frozenset[str] = frozenset(), path: str = "") -> list[str]:
    """Paths of the values the source does not support. `path` is "" for a plain field, and names
    the element inside a list or an object ("0", "0.pol"). Leaves named in `exempt` are skipped."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, dict):
        return [found for key, inner in value.items() if key not in exempt
                for found in unsupported(inner, source, exempt, f"{path}.{key}" if path else str(key))]
    if isinstance(value, (list, tuple)):
        return [found for index, inner in enumerate(value)
                for found in unsupported(inner, source, exempt, f"{path}.{index}" if path else str(index))]
    if isinstance(value, (int, float)):
        return [] if source.has_number(float(value)) else [path]
    text = str(value).strip()
    if not text:
        return []
    if _ISO_DATE.match(text):
        try:
            return [] if source.has_date(date.fromisoformat(text)) else [path]
        except ValueError:
            pass                                          # not a real date: hold it to the text instead
    return [] if source.has_text(text) else [path]
