"""Input checks: run on the email before any model sees it. Each returns its failures; empty means it passed.

To add one: write a function here, add it to CHECKS, and add its name to "input" -> "checks" in rules.json.

Every check that matches text does so against the views in normalize.py, never against the raw
string alone, so a disguise, an encoding or an OCR misreading does not get a payload past it.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Callable, Iterator

from . import classifier, deadline
from .normalize import (INVISIBLE, OCR_WATCH_WORDS, SMUGGLING, decoded_segments, decoded_views,  # noqa: F401
                        joined_view, longest_encoded_block, ocr_view, plain_view, readable_view, unmasked_view,
                        visible)

_LATIN = re.compile(r"[A-Za-z]")
_OTHER_SCRIPT = re.compile(r"[Ͱ-ϿЀ-ӿ]")
_WORD = re.compile(r"\w+")

# How much of each neighbour is looked at when checking for an instruction that starts at the end
# of one source and finishes at the start of the next (the email body and its first attachment, say).
_SEAM = 240


def clean(text: str) -> str:
    """What the model is given: what cannot be seen is removed, and nothing that can be seen is changed."""
    return visible(text)


def _parts(req, parts=("question", "body", "attachment")) -> Iterator[tuple[str, str, str]]:
    """(kind, name, text) for each non-empty part of the request a check reads."""
    if req.question and "question" in parts:
        yield "question", "question", req.question
    if req.body and "body" in parts:
        yield "body", "body", req.body
    if "attachment" in parts:
        for a in req.attachments:
            yield "attachment", a.name, a.text


def _sources(req, parts=("question", "body", "attachment")) -> Iterator[tuple[str, str]]:
    for _, name, text in _parts(req, parts):
        yield name, text


def size_limit(req, config) -> list[dict]:
    """Length (open questions Q6 and Q25, answered 3 Oct). A question over its cap is stopped: a
    9,000-character "question" is not a question, and the user gets the fallback. Mail over a cap,
    one part or everything together, goes to a person: an email is not an attack for being long.
    Length is a reason to look, not a warning sign about the sender, so it never adds up towards a
    block, and a long email is still read by every other check."""
    findings = []
    for kind, source, text in _parts(req, config.parts("size_limit")):
        cap = config.max_part_chars.get(kind)
        if cap is not None and len(text) > cap:
            findings.append({"source": source, "action": "block" if kind == "question" else "review",
                             "rule": f"size_limit.{kind}", "escalates": False,
                             "message": f"{len(text):,} characters; the limit for one {kind} is {cap:,}"})
    total = len(req.question) + len(req.body) + sum(len(a.text) for a in req.attachments)
    if total > config.max_chars:
        findings.append({"source": None, "action": "review", "rule": "size_limit.total", "escalates": False,
                         "message": f"{total:,} characters in total; the limit is {config.max_chars:,}"})
    # The sizes themselves, so that different caps can be tried against recorded traffic.
    return findings + [{"action": "allow", "measures": {
        "question_chars": len(req.question), "body_chars": len(req.body), "total_chars": total,
        "longest_attachment_chars": max((len(a.text) for a in req.attachments), default=0)}}]


def ingest(req, config) -> list[dict]:
    """What the converters upstream say they could not read. The PDF converter puts one marker line
    in place of each scanned page it could not OCR, so that the page is not silently empty. A
    document carrying a marker goes to a person: the page exists and nobody has read it.

    It matches the converter's own line as a person would read it, and nothing looser: no disguise
    is undone and nothing is decoded, because a converter does not disguise its own marker. Review
    only. It is not a warning sign about the sender, so it never adds up towards a block."""
    findings, pages = [], 0
    for _, source, text in _parts(req, config.parts("ingest")):
        view = plain_view(text)
        for rule in config.ingest.rules:
            matches = [match for pattern in rule.compiled for match in pattern.finditer(view)]
            if not matches:
                continue
            pages += len(matches)
            findings.append({"source": source, "rule": rule.id, "action": "review", "escalates": False,
                             "message": f"{rule.description} ({len(matches)} in this document)",
                             **_where(matches[0], "plain")})
    return findings + [{"action": "allow", "measures": {"pages_unreadable": pages}}]


def hidden_characters(req, config) -> list[dict]:
    failures = []
    for source, text in _sources(req, config.parts("hidden_characters")):
        smuggled, invisible = len(SMUGGLING.findall(text)), len(INVISIBLE.findall(text))
        mixed = [w for w in _WORD.findall(text) if _LATIN.search(w) and _OTHER_SCRIPT.search(w)]
        if smuggled:
            failures.append({"source": source, "action": config.hidden_characters.tag_characters,
                             "message": f"{smuggled} invisible tag characters, a known way to hide instructions"})
        if invisible:
            failures.append({"source": source, "action": config.hidden_characters.invisible,
                             "message": f"{invisible} zero-width or direction-control characters"})
        if mixed:
            failures.append({"source": source, "action": config.hidden_characters.invisible,
                             "message": f"words mixing Latin with look-alike Cyrillic or Greek letters, "
                                        f"e.g. '{mixed[0][:30]}'"})
    return failures


def _first_match(rule, text: str, crossing: int | None = None):
    """The first match of a rule in `text`. Rules carry their own `unless` guard; scope rules don't."""
    if hasattr(rule, "search"):
        return rule.search(text, crossing)
    return next((m for m in (p.search(text) for p in rule.compiled) if m), None)


def _where(match, view: str) -> dict:
    """Where a rule matched, for the log: offsets within the named view of the text, and a hash of
    the matched text. The log gets these and never the text itself."""
    return {"span": match.span(), "view": view,
            "matched_hash": hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:12]}


def _views(text: str, config) -> list[tuple[str, str]]:
    """The views a rule is tried against, in order. A later view is only listed when it differs
    from the ones before it, so plain text costs one pass."""
    plain, unmasked = plain_view(text), unmasked_view(text)
    views = [("plain", plain)]
    if unmasked != plain:
        views.append(("unmasked", unmasked))
    ocr = ocr_view(text, tuple(OCR_WATCH_WORDS) + tuple(config.ocr_watch_words))
    if ocr != unmasked:
        views.append(("ocr", ocr))
    joined = joined_view(text)
    if joined != unmasked:
        views.append(("joined", joined))
    return views


def _reads(rule, kind: str) -> bool:
    return rule.applies_to is None or kind in rule.applies_to


def _scan(rules, sources: list[tuple[str, str, str]], config, *, disguise_signal: bool, decoded_action: str | None):
    """Tries every rule against every view of every source. Shared by the checks that are rule sets.

    `disguise_signal`: add a `disguised_text` finding when a rule only matched once disguises were
    undone. `decoded_action`: what a match inside decoded base64, hex or ROT13 becomes (None = the
    rule's own action).
    """
    failures = []
    for kind, source, text in sources:
        disguised = []
        for rule in rules:
            deadline.check()
            if not _reads(rule, kind):
                continue
            for view, content in _views(text, config):
                match = _first_match(rule, content)
                if match:
                    # Matching only once disguises are undone is itself a warning sign. Matching only
                    # after an OCR correction or across a line break is not: OCR noise and wrapped
                    # lines are ordinary.
                    if view == "unmasked":
                        disguised.append(rule.id)
                    failures.append({"source": source, "rule": rule.id, "action": rule.action,
                                     "weight": rule.weight, "matched": match.group(0)[:80],
                                     "message": rule.description, **_where(match, view)})
                    break
        if disguised and disguise_signal:
            failures.append({"source": source, "rule": "disguised_text", "action": "review",
                             "message": f"only matched once disguises were removed: {', '.join(disguised)}"})
        for decoded in decoded_views(text):
            for view in (plain_view(decoded.text), unmasked_view(decoded.text)):
                hits = [(r, m) for r in rules if _reads(r, kind) and (m := _first_match(r, view))]
                if hits:
                    rule, match = hits[0]
                    failures.append({"source": f"{source} (decoded)", "rule": rule.id,
                                     "action": decoded_action or rule.action, "weight": rule.weight,
                                     "matched": match.group(0)[:80],
                                     "message": f"{decoded.kind} text that decodes to: {rule.description.lower()}",
                                     **_where(match, "decoded")})
                    break

    # A phrase that starts at the end of one source and ends at the start of the next.
    found = {(f["source"], f["rule"]) for f in failures}
    for (kind_before, _, before), (kind, source, after) in zip(sources, sources[1:]):
        head = joined_view(before)[-_SEAM:]
        seam = head + " " + joined_view(after)[:_SEAM]
        for rule in rules:
            if not (_reads(rule, kind_before) and _reads(rule, kind)):
                continue
            match = _first_match(rule, seam, crossing=len(head))
            if match and (source, rule.id) not in found:
                failures.append({"source": source, "rule": rule.id, "action": rule.action, "weight": rule.weight,
                                 "matched": match.group(0)[:80],
                                 "message": f"{rule.description} (split across two parts of the message)",
                                 **_where(match, "seam")})
    return failures


def injection_rules(req, config) -> list[dict]:
    """Instructions aimed at the model. Text hidden in an encoding is an attack whatever it says."""
    return _scan(config.injection_rules, list(_parts(req, config.parts("injection_rules"))), config,
                 disguise_signal=True, decoded_action="block")


def business_risk(req, config) -> list[dict]:
    """Business-email compromise: changed bank details, a different consignee or delivery address,
    release without the original bill of lading, pressure not to verify. Asks for a person. It never
    blocks and never adds to the count of warning signs: these are requests a real customer makes
    too, and what makes them dangerous is acting on them unchecked."""
    sources = list(_parts(req, config.parts("business_risk")))
    return [{**f, "action": "review", "escalates": False}
            for f in _scan(config.business_risk.rules, sources, config, disguise_signal=False, decoded_action=None)]


def injection_classifier(req, config) -> list[dict]:
    """A learned detector for instructions aimed at a model. It reads what a person would read, the
    same text with character-level disguises undone when that differs (numbers and punctuation are
    left alone: see readable_view), and anything decoded from an encoding; a source's score is the
    highest of those. At or above the review threshold a person looks; at or
    above the block threshold, if the policy sets one, the model is not called.

    The last entry returned is not a finding. It carries the highest score seen, so that a request
    the classifier passed still has a score on record."""
    params = config.injection_classifier
    scorer = classifier.get_scorer(params)
    findings, highest = [], 0.0
    for source, text in _sources(req, config.parts("injection_classifier")):
        candidates = [("visible", visible(text))]
        if readable_view(text) != candidates[0][1]:
            candidates.append(("readable", readable_view(text)))
        candidates += [("decoded", decoded.text) for decoded in decoded_views(text)]
        best = (0.0, None, "visible", "")
        for view, content in candidates:
            if content.strip():
                deadline.check()
                score, span = scorer.score(content)
                if score > best[0]:
                    best = (score, span, view, content)
        score, span, view, content = best
        highest = max(highest, score)
        if params.block_threshold is not None and score >= params.block_threshold:
            action = "block"
        elif score >= params.review_threshold:
            action = "review"
        else:
            continue
        finding = {"source": f"{source} (decoded)" if view == "decoded" else source,
                   "rule": f"injection_classifier.{action}", "action": action, "score": score,
                   "weight": params.escalation_weight,
                   "message": f"the injection classifier scored this {score:.2f}"}
        if span:
            finding.update(span=span, view=view,
                           matched_hash=hashlib.sha256(content[span[0]:span[1]].encode("utf-8")).hexdigest()[:12])
        findings.append(finding)
    return findings + [{"action": "allow", "rule": "injection_classifier.score", "score": highest,
                        "measures": {"score": round(highest, 6)}}]


def encoded_content(req, config) -> list[dict]:
    failures, longest_seen = [], 0
    for source, text in _sources(req, config.parts("encoded_content")):
        longest = longest_encoded_block(text)
        longest_seen = max(longest_seen, longest)
        if longest >= config.encoded_content.min_length:
            failures.append({"source": source, "action": config.encoded_content.action,
                             "message": f"a {longest}-character encoded block, which could hide text"})
    return failures + [{"action": "allow", "measures": {"longest_encoded_block": longest_seen}}]


def scope(req, config) -> list[dict]:
    """Clear out-of-scope requests (write code, a poem, a joke) are refused before the model is called.
    It reads what is being asked: the question. A request with no question at all is its own
    question, so then the body is read (open question Q5, approved). Inbound mail sent with a task is
    never refused for what the mail says. Subtler cases are left to the model's instructions, and its
    reply is checked afterwards."""
    parts = config.parts("scope")
    sources = list(_sources(req, parts))
    if not req.question and "question" in parts and "body" not in parts and req.body:
        sources.insert(0, ("body", req.body))
    for source, text in sources:
        for name, view in (("plain", plain_view(text)), ("unmasked", unmasked_view(text))):
            for rule in config.scope_rules:
                match = _first_match(rule, view)
                if match:
                    return [{"source": source, "rule": rule.id, "action": "refuse",
                             "matched": match.group(0)[:80], "message": rule.description, **_where(match, name)}]
    return []


CHECKS: dict[str, Callable] = {
    "size_limit": size_limit,
    "ingest": ingest,
    "hidden_characters": hidden_characters,
    "injection_rules": injection_rules,
    "injection_classifier": injection_classifier,
    "encoded_content": encoded_content,
    "scope": scope,
    "business_risk": business_risk,
}


def _classifier_ready(req, config) -> None:
    """Can the classifier run? Loads the model if it is not loaded yet; raises if it cannot be."""
    classifier.get_scorer(config.injection_classifier)


# For a shadow check the policy runs after the response: what must hold BEFORE the answer is given
# for the check to be able to run at all. If it does not hold, that is a review now.
READINESS: dict[str, Callable] = {"injection_classifier": _classifier_ready}


def wrap(req, template: str) -> dict:
    """What to send to the model: the instructions as the system prompt, and a user message with the
    request and the documents fenced by a random marker the sender can't guess, so they can't close
    the fence and write outside it."""
    marker = f"DATA_{secrets.token_hex(6)}"
    parts = ([f"[email]\n{clean(req.body)}"] if req.body.strip() else []) + \
            [f"[attachment: {a.name}]\n{clean(a.text)}" for a in req.attachments]
    documents = f"{marker}\n" + ("\n\n".join(parts) or "(no documents)") + f"\n{marker}"
    request = clean(req.question) if req.question.strip() else "Extract the requested fields from the documents."
    return {"system_instructions": template.replace("{marker}", marker),
            "user_message": f"Request: {request}\n\nDocuments:\n{documents}",
            "marker": marker}
