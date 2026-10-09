# COPIED FROM backend/app/services/guardrail_gateway.py BY sync_guardrail_flow.py. DO NOT EDIT HERE.
#
# Not covered by the LICENSE in this folder: this file belongs to the Logistics-AI-Platform
# repository. tests/test_copied_flow.py fails when it is not exactly its source; to take a
# change, run `python sync_guardrail_flow.py`.
"""
app/services/guardrail_gateway.py

The one place the backend talks to the guardrail service (libs/guardrail).

--- WHAT THE GUARDRAIL IS ---

A small HTTP service that sits BEFORE and AFTER every model call:

    email text ─> POST /guardrail/input  ─> model ─> POST /guardrail/output  (an extraction)
    question   ─> POST /guardrail/input  ─> model ─> POST /guardrail/answer  (a chat reply)

It never calls a model. It screens what is about to be shown to one (prompt
injection, hidden characters, out-of-scope requests) and checks what came
back (schema, confidence, values the source does not support, leaked
instructions).

--- THE TWO RULES THIS MODULE EXISTS TO ENFORCE ---

1. NEVER BYPASS. If the guardrail cannot be reached -- not configured, a
   timeout, a refused connection, a non-200 answer, a body that is not the
   agreed JSON -- the result is REVIEW (or, for a chat reply, a withheld
   answer). It is never "pass". A screening step that waves everything
   through whenever it is down is not a screening step, and an attacker who
   can make it slow should gain nothing by doing so.

2. ALWAYS ACT ON THE VERDICT. Callers do exactly what the returned status
   says, every time. Whether a given check is enforced or merely observed
   ("shadow mode") is decided INSIDE the guardrail, by its policy: in shadow
   mode it records what it would have done and answers "pass". So turning
   enforcement on later is a policy change there, and needs no change, no
   flag and no deploy here.

--- WHAT IS LOGGED ---

Request ids, statuses and reason CODES (rule and check names). Never the
email, a filename, an extracted value, a question or an answer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import httpx

from .settings import settings

logger = logging.getLogger("guardrail-gateway")

# What the model is being asked to do with an email, sent as the `question`.
# The guardrail's scope check reads the question; without one it would read
# the EMAIL BODY as if it were the request, and refuse a customer's email for
# containing the words "write a script".
DOCUMENT_TASK = "Classify this email and extract the freight details it states."

# Shown in place of a chat reply that could not be checked.
ANSWER_WITHHELD = "Sorry, I can't answer right now. Please try again shortly."

_INPUT_STATUSES = {"pass", "review", "fail", "out_of_scope"}
_OUTPUT_STATUSES = {"pass", "review", "fail"}
_ANSWER_STATUSES = {"answered", "no_answer", "out_of_scope"}

# Tests swap this for an httpx.MockTransport. Production leaves it None.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class Finding:
    """
    One reason the guardrail gave, as codes only: the rule or check that
    fired, what it asked for, and which part of the request it was in
    ("question", "body", an attachment's name, or None for the request as a
    whole). The guardrail's `matched` and `message` are never kept: they can
    quote the email, and they are neither logged nor shown (open question Q3).
    """

    code: str
    check: str
    action: str
    source: str | None = None


@dataclass(frozen=True)
class InputVerdict:
    """
    What to do with text that is about to go to a model.

    pass          call the model
    review        call the model, and put the result in front of a person
    fail          do NOT call the model; quarantine (an attack was detected)
    out_of_scope  do NOT call the model; show `message`
    """

    status: str
    reasons: tuple[str, ...] = ()
    message: str | None = None
    unavailable: bool = False
    findings: tuple[Finding, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.status in ("fail", "out_of_scope")

    @property
    def needs_review(self) -> bool:
        return self.status == "review"


@dataclass(frozen=True)
class OutputVerdict:
    """
    What to do with an extraction.

    pass    use it
    review  keep it, and put it in front of a person
    fail    keep it for the reviewer, trust none of it

    `fields` holds the values as the guardrail returned them, when it returned
    any. The only edit a caller applies from it is REMOVING a value (see
    without_unsupported_values); it never adds or rewrites one.
    """

    status: str
    reasons: tuple[str, ...] = ()
    fields: Mapping[str, Any] | None = None
    unavailable: bool = False

    @property
    def needs_review(self) -> bool:
        return self.status != "pass"


@dataclass(frozen=True)
class AnswerVerdict:
    """`answer` is always safe to show: the checked reply, or what replaces it."""

    status: str        # answered | no_answer | out_of_scope | withheld
    answer: str
    reasons: tuple[str, ...] = ()
    unavailable: bool = False

    @property
    def answered(self) -> bool:
        return self.status == "answered"


class _Unavailable(Exception):
    """The guardrail gave no usable answer. `reason` is a code, never text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _base_url() -> str:
    return (settings.guardrail_url or "").strip().rstrip("/")


async def _post(path: str, payload: dict, *, request_id: str) -> dict:
    """
    One call to the guardrail. Returns its JSON body, or raises _Unavailable
    with a reason code. Every way this can go wrong ends in _Unavailable, so
    no caller can forget a case and fall through to "pass".
    """
    base = _base_url()
    if not base:
        raise _Unavailable("guardrail_not_configured")
    timeout_s = max(settings.guardrail_timeout_ms, 1) / 1000.0
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=_transport) as client:
            response = await client.post(f"{base}{path}", json=payload)
    except httpx.TimeoutException:
        raise _Unavailable("guardrail_timeout") from None
    except httpx.HTTPError:
        raise _Unavailable("guardrail_unreachable") from None
    if response.status_code != 200:
        # 422 means OUR request was malformed -- a bug here, not an outage --
        # but the email in hand still has to go somewhere safe.
        raise _Unavailable(f"guardrail_http_{response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise _Unavailable("guardrail_bad_response") from None
    if not isinstance(body, dict) or not isinstance(body.get("status"), str):
        raise _Unavailable("guardrail_bad_response")
    return body


def _reasons(body: Mapping[str, Any]) -> tuple[str, ...]:
    """Reason codes from a response's failures: rule or check names, plus the field."""
    codes: list[str] = []
    for failure in body.get("failures") or []:
        if not isinstance(failure, Mapping):
            continue
        code = str(failure.get("rule") or failure.get("check") or "unknown")
        if failure.get("field"):
            code = f"{code}:{failure['field']}"
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def _findings(body: Mapping[str, Any]) -> tuple[Finding, ...]:
    """The response's failures as codes and parts. Nothing that can quote text."""
    found: list[Finding] = []
    for failure in body.get("failures") or []:
        if not isinstance(failure, Mapping):
            continue
        source = failure.get("source")
        found.append(Finding(
            code=str(failure.get("rule") or failure.get("check") or "unknown"),
            check=str(failure.get("check") or "unknown"),
            action=str(failure.get("action") or "review"),
            source=str(source) if isinstance(source, str) else None,
        ))
    return tuple(found)


def _log_unavailable(stage: str, request_id: str, reason: str) -> None:
    logger.warning(
        "Guardrail %s check for %s could not run (%s); treated as review, not as a pass.",
        stage, request_id, reason,
    )


def _attachments(named_texts: Sequence[tuple[str | None, str | None]]) -> list[dict]:
    """
    The guardrail's attachment shape. A name is required and must be
    non-empty; text may be empty (a scan with nothing readable).
    """
    return [
        {"name": (name or "").strip() or f"attachment-{index}", "text": text or ""}
        for index, (name, text) in enumerate(named_texts, start=1)
    ]


async def _check_input(request_id: str, payload: dict) -> InputVerdict:
    try:
        body = await _post("/guardrail/input", {"request_id": request_id, **payload}, request_id=request_id)
    except _Unavailable as exc:
        _log_unavailable("input", request_id, exc.reason)
        return InputVerdict(status="review", reasons=(exc.reason,), unavailable=True)
    status = body["status"]
    if status not in _INPUT_STATUSES:
        _log_unavailable("input", request_id, "guardrail_unknown_status")
        return InputVerdict(status="review", reasons=("guardrail_unknown_status",), unavailable=True)
    reasons = _reasons(body)
    logger.info("Guardrail input %s: %s %s", request_id, status, ",".join(reasons) or "-")
    return InputVerdict(status=status, reasons=reasons, message=body.get("message"), findings=_findings(body))


async def check_document(
    request_id: str,
    *,
    subject: str | None,
    body_text: str | None,
    attachments: Sequence[Any] = (),
) -> InputVerdict:
    """
    Screens one email -- subject, body and every attachment's text -- before
    any model reads it. `attachments` are OcrAttachmentText (or anything with
    `filename` and `text`).
    """
    body = body_text or ""
    if subject and subject.strip():
        body = f"Subject: {subject.strip()}\n\n{body}"
    return await _check_input(request_id, {
        "question": DOCUMENT_TASK,
        "body": body,
        "attachments": _attachments([(a.filename, a.text) for a in attachments]),
    })


async def check_question(
    request_id: str,
    question: str,
    excerpts: Sequence[Mapping[str, Any]] = (),
) -> InputVerdict:
    """
    Screens a staff member's question, and the vault excerpts the model was
    given to answer it. Excerpts are customer and vendor mail, so they are
    untrusted in exactly the way an inbound email is.
    """
    return await _check_input(request_id, {
        "question": question,
        "attachments": _attachments([
            (str(e.get("source_uri") or ""), str(e.get("text") or "")) for e in excerpts
        ]),
    })


async def check_extraction(
    request_id: str,
    *,
    intent: str,
    fields: Mapping[str, Any],
    confidences: Mapping[str, float],
    overall_confidence: float | None,
) -> OutputVerdict:
    """
    Checks what the model extracted. `confidences` and `overall_confidence`
    are this codebase's PERCENTAGES (0-100); the guardrail takes fractions.
    """
    def fraction(percentage: float | None) -> float | None:
        if percentage is None:
            return None
        return max(0.0, min(1.0, float(percentage) / 100.0))

    payload = {
        "request_id": request_id,
        "intent": intent,
        "fields": {
            name: {"value": value, "confidence": fraction(confidences.get(name))}
            for name, value in fields.items()
        },
        "overall_confidence": fraction(overall_confidence),
    }
    try:
        body = await _post("/guardrail/output", payload, request_id=request_id)
    except _Unavailable as exc:
        _log_unavailable("output", request_id, exc.reason)
        return OutputVerdict(status="review", reasons=(exc.reason,), unavailable=True)
    status = body["status"]
    if status not in _OUTPUT_STATUSES:
        _log_unavailable("output", request_id, "guardrail_unknown_status")
        return OutputVerdict(status="review", reasons=("guardrail_unknown_status",), unavailable=True)
    reasons = _reasons(body)
    logger.info("Guardrail output %s: %s %s", request_id, status, ",".join(reasons) or "-")
    returned = body.get("output")
    return OutputVerdict(
        status=status, reasons=reasons, fields=returned if isinstance(returned, Mapping) else None,
    )


async def check_answer(request_id: str, answer: str, *, question: str = "") -> AnswerVerdict:
    """
    Checks a chat reply before anyone sees it. The reply that comes back is
    what to show. If the check cannot run, the model's reply is NOT shown.
    """
    payload = {"request_id": request_id, "answer": answer or "", "question": question}
    try:
        body = await _post("/guardrail/answer", payload, request_id=request_id)
    except _Unavailable as exc:
        _log_unavailable("answer", request_id, exc.reason)
        return AnswerVerdict(status="withheld", answer=ANSWER_WITHHELD, reasons=(exc.reason,), unavailable=True)
    status, shown = body["status"], body.get("answer")
    if status not in _ANSWER_STATUSES or not isinstance(shown, str) or not shown.strip():
        _log_unavailable("answer", request_id, "guardrail_bad_response")
        return AnswerVerdict(
            status="withheld", answer=ANSWER_WITHHELD, reasons=("guardrail_bad_response",), unavailable=True,
        )
    failed = tuple(
        str(check.get("check")) for check in body.get("checks") or []
        if isinstance(check, Mapping) and check.get("passed") is False
    )
    logger.info("Guardrail answer %s: %s %s", request_id, status, ",".join(failed) or "-")
    return AnswerVerdict(status=status, answer=shown, reasons=failed)


def without_unsupported_values(fields: Mapping[str, Any], verdict: OutputVerdict) -> dict[str, Any]:
    """
    The extraction with any value the guardrail REMOVED set to None.

    Removing is the only edit accepted from the guardrail: it nulls a value
    the source text does not support. A value it changed, or a key it added
    (a default, say), is ignored here -- the extraction stays the model's own
    words, minus what could not be stood behind.
    """
    cleaned = dict(fields)
    if verdict.fields is None:
        return cleaned
    for name, value in fields.items():
        if value is not None and name in verdict.fields and verdict.fields[name] is None:
            cleaned[name] = None
    return cleaned


# --------------------------------------------------------------------------
# Chat: what a review means depends on what triggered it
# --------------------------------------------------------------------------
#
# A PRODUCT DECISION FOR THE TEAM TO CONFIRM (guardrail open question Q2,
# answered by the guardrail owner on 3 Oct 2026). Chat has no review queue,
# so "review" is turned into something a user can see:
#
#   business-risk signal in a source email   answer, with a one-line caution
#                                            naming the reason in plain words
#   injection signal in a source email       no answer: the fallback, and a
#                                            note that a source email was
#                                            flagged, with its enquiry
#                                            reference when it has one
#   review on the question itself            answer, with a caution
#   a page in a source document that the     answer, with one line saying the
#   converter could not read                 answer may be incomplete
#   block, or the answer check withholding   the fallback
#
# Anything else -- the guardrail unreachable, a reason this code does not
# know -- is the fallback. Never a bypass.

# The checks whose findings mean "something tried to instruct the model".
_INJECTION_CHECKS = {"injection_rules", "injection_classifier", "hidden_characters", "encoded_content",
                     "escalation"}

# Business-risk rules by family, in the words a user is shown. Three rules in
# the guardrail's injection set describe the same frauds: the older payment
# rule, and the status-change rule, which the guardrail has in English
# (shipment_manipulation) and in Hindi and Hinglish
# (shipment_manipulation_hindi). A rule the guardrail has in more than one
# language is listed under every one of its ids, so that what a user is shown
# does not depend on the language the source email was written in. A rule id
# that is in no list here is "a reason this code does not know" and withholds
# the reply: that is where a new guardrail rule starts until it is listed.
_BUSINESS_RISK = (
    (("bec.bank_change", "payment_detail_change"), "a source email asks to change bank or payment details"),
    (("bec.party_change",), "a source email asks to change the consignee, notify party or delivery address"),
    (("bec.release_without_documents",), "a source email asks for cargo to be released without the original documents"),
    (("bec.pressure",), "a source email presses for action without the usual checks"),
    (("shipment_manipulation", "shipment_manipulation_hindi"),
     "a source email asks to change the status of a shipment, invoice or payment"),
)

# A scanned page the converter could not read (the guardrail's rule
# `ingest.page_unreadable`; guardrail open question Q56, answered by the
# guardrail owner on 3 Oct 2026, FOR THIS TEAM TO CONFIRM alongside Q2). An
# unreadable page makes an answer incomplete, not dangerous, so the answer is
# shown with this line -- once, however many pages or documents are marked.
# The guardrail enforces that rule from its first day, in its rollout policy
# too, so without this a marked document among the excerpts would be "a
# reason this code does not know" and would withhold the reply.
_UNREADABLE_PAGE = "ingest.page_unreadable"
UNREADABLE_PAGE_CAUTION = "A page in one of the source documents couldn't be read, so this answer may be incomplete."

_QUESTION_REASONS = {
    "hidden_characters": "it contains hidden characters",
    "encoded_content": "it contains an encoded block",
}


def _business_risk_reason(code: str) -> str | None:
    for prefixes, words in _BUSINESS_RISK:
        if any(code == prefix or code.startswith(f"{prefix}.") for prefix in prefixes):
            return words
    return None


@dataclass(frozen=True)
class ChatDecision:
    """`answer` False means show `fallback`; True means show the model's
    checked reply followed by `cautions`, one per line."""

    answer: bool
    cautions: tuple[str, ...] = ()
    fallback: str = ANSWER_WITHHELD


def decide_question(verdict: InputVerdict) -> ChatDecision:
    """The check on the question alone, before the model is asked."""
    if verdict.blocked:
        return ChatDecision(answer=False, fallback=verdict.message or ANSWER_WITHHELD)
    if verdict.status == "pass":
        return ChatDecision(answer=True)
    if verdict.unavailable or not verdict.findings:
        return ChatDecision(answer=False)
    if all(f.source == "question" and f.action == "review" for f in verdict.findings):
        why = _QUESTION_REASONS.get(verdict.findings[0].check, "it matched a pattern the guardrail watches for")
        return ChatDecision(answer=True, cautions=(f"Caution: your question was flagged for review because {why}. "
                                                   "Treat this answer with care.",))
    return ChatDecision(answer=False)


def decide_excerpts(verdict: InputVerdict, excerpts: Sequence[Mapping[str, Any]] = ()) -> ChatDecision:
    """The check on the question with every excerpt the model read."""
    if verdict.blocked or verdict.unavailable:
        return ChatDecision(answer=False)
    if verdict.status == "pass":
        return ChatDecision(answer=True)
    if not verdict.findings:
        return ChatDecision(answer=False)                     # a review with no reason given
    reference = {str(e.get("source_uri") or ""): e.get("enquiry_ref") for e in excerpts}
    in_mail = [f for f in verdict.findings if f.source not in (None, "question")]
    # The older payment and status rules live in the guardrail's injection set but describe a fraud
    # in the mail, not an attempt to instruct the model: they are business risk here.
    injected = [f for f in in_mail if f.check in _INJECTION_CHECKS and _business_risk_reason(f.code) is None]
    if injected:
        refs = sorted({ref for f in injected if (ref := reference.get((f.source or "").removesuffix(" (decoded)")))})
        where = f" (enquiry {', '.join(refs)})" if refs else ""
        return ChatDecision(answer=False, fallback=f"{ANSWER_WITHHELD} A source email for this question was "
                                                   f"flagged for review{where}, so the answer is not shown.")
    cautions: list[str] = []
    for finding in verdict.findings:
        if finding.code == _UNREADABLE_PAGE and finding.action == "review" and finding.source not in (None, "question"):
            if UNREADABLE_PAGE_CAUTION not in cautions:
                cautions.append(UNREADABLE_PAGE_CAUTION)
            continue
        words = _business_risk_reason(finding.code) if finding.source not in (None, "question") else None
        if words is None and finding.source == "question" and finding.action == "review":
            words = "your question was flagged for review"
        if words is None:
            return ChatDecision(answer=False)                 # a reason this code does not know
        ref = reference.get(finding.source or "")
        line = f"Caution: {words}{f' (enquiry {ref})' if ref else ''}. Confirm it before acting on it."
        if line not in cautions:
            cautions.append(line)
    return ChatDecision(answer=True, cautions=tuple(cautions))


def with_cautions(reply: str, cautions: Sequence[str]) -> str:
    """The reply with the cautions appended, so no frontend change is needed."""
    return "\n\n".join([reply, *cautions]) if cautions else reply


def review_reason(stage: str, status: str, reasons: Sequence[str]) -> str:
    """The sentence a reviewer sees. Codes only -- it is stored and displayed."""
    what = {
        "input": "the email's text",
        "output": "the extracted fields",
    }.get(stage, stage)
    if status == "fail":
        lead = f"Guardrail stopped this: {what} did not pass its checks"
    else:
        lead = f"Guardrail asked for a person to check {what}"
    return f"{lead} ({', '.join(reasons)})." if reasons else f"{lead}."
