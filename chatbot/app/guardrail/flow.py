# COPIED FROM libs/guardrail/testbench/bench/flow.py (branch feat/guardrail-testbench) BY sync_guardrail_flow.py. DO NOT EDIT HERE.
#
# Not covered by the LICENSE in this folder: this file belongs to the Logistics-AI-Platform
# repository. tests/test_copied_flow.py fails when it is not exactly its source; to take a
# change, run `python sync_guardrail_flow.py`.
"""One test, run the way the backend runs an email or a chat question.

Two flows, each following its backend caller step for step, through the backend's own gateway
functions (bench/gateway.py):

  extraction   An inbound email (cg-intent, then cg-extraction).
               1. check_document: the email and every attachment, with the fixed task the backend
                  sends. Stopped: no model reads it, and a review task says why.
               2. The model extracts the fields of the chosen intent.
               3. check_extraction, under the SAME request id. Anything but a pass opens a review
                  task; a value the guardrail removed is not stored.

  chat         A question about source emails (POST /chat).
               1. check_question on the question alone, under `chat-<id>`.
               2. check_question on the question with the excerpts, under `chat-<id>:read`.
               3. The model answers.
               4. check_answer on the reply, under `chat-<id>:read`.
               What the user is shown is decided by the backend's decide_question,
               decide_excerpts and with_cautions.

Where the testbench is NOT the backend, and why:

  * The prompt. The backend keeps its own prompts and ignores the one the guardrail returns. The
    testbench sends the guardrail's (`system_instructions` and `user_message`), because that is
    what it is here to try.
  * The excerpts. The backend's model searches a vault and chooses its excerpts, so they can only
    be screened after it has answered. Here the tester supplies them (the pasted email body and
    attachments), so they are screened first and a flagged excerpt saves the model call. What the
    user is shown is the same either way.
  * Confidence. The backend scores each extracted field from the model's token probabilities with
    its own code. The testbench does not: it sends the fields unscored, and the guardrail's
    `confidence` check will say so.

Every request id is new for every test, so the guardrail never sees different text under an id it
already knows (`source_conflict`).
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from . import gateway
from .guardrail_io import Exchange, call
from .model import Model

EXTRACTION, CHAT = "extraction", "chat"
MODES = {EXTRACTION: "An inbound email (extraction)", CHAT: "A question about source emails (chat)"}
# The name the pasted email body is sent under in chat, where the backend sends excerpts and no body.
BODY_EXCERPT = "email:body"
NOT_SHOWN = "[not shown by the testbench]"

# What a test came to, for scoring. Worst first.
STOPPED, REVIEW, PASS, NOT_SCORED = "fail", "review", "pass", "not scored"


@dataclass
class Case:
    mode: str                                   # EXTRACTION or CHAT
    body: str = ""
    attachments: list[dict] = field(default_factory=list)      # [{"name": ..., "text": ...}]
    subject: str = ""                           # extraction: sent as the backend sends it, at the top of the body
    question: str = ""                          # chat
    intent: str = ""                            # extraction


@dataclass
class Call:
    """One call to the guardrail, as the tester's panel shows it. Never holds `matched`."""

    label: str
    stage: str                                  # input | output | answer
    request_id: str
    status: str | None                          # the guardrail's own status; None when it gave no usable answer
    unavailable: str | None                     # the gateway's reason code when the guardrail could not be used
    rules_version: str | None
    checks: list[dict]                          # [{"check", "passed"}]: the enforced checks that ran
    findings: list[dict]                        # [{"rule", "check", "action", "source", "field", "message"}]
    notes: list[str]                            # answer stage: what the guardrail did to the reply
    raw: dict | None                            # the response, `matched` taken out


@dataclass
class Outcome:
    kind: str                                   # see KINDS
    title: str
    shown: str | None = None                    # chat: the exact text a user would see
    detail: list[str] = field(default_factory=list)


KINDS = {
    "answer": "The answer is shown",
    "answer_with_caution": "The answer is shown, with a caution",
    "fallback": "The reply is withheld; the user sees a fallback",
    "processed": "The email is processed normally",
    "review": "The email is processed, and a person is asked to check it",
    "stopped": "The email is stopped before any model reads it",
    "prompt_ready": "Guardrail only: the model would be called now",
    "held_for_review": "The model is not called: the check before it did not pass",
    "provider_refusal": "The model provider refused the request",
    "model_error": "The model call failed",
}


@dataclass
class Run:
    id: str
    started_at: str
    test: Case
    request_ids: list[str]
    calls: list[Call]
    prompt: dict | None                         # {"system_instructions", "user_message"} the guardrail returned
    model_mode: str
    model_called: bool
    model_reply: str | None
    outcome: Outcome
    result: str                                 # STOPPED, REVIEW, PASS or NOT_SCORED
    policy: str | None

    @property
    def rules_fired(self) -> list[str]:
        return list(dict.fromkeys(f["rule"] or f["check"] for c in self.calls for f in c.findings))


def without_matched(body: dict | None) -> dict | None:
    """The guardrail's response with `matched` taken out of every failure: it is the text a rule
    matched, so it can quote the email. The backend neither logs nor shows it; nor does this."""
    if body is None:
        return None
    shown = copy.deepcopy(body)
    for failure in shown.get("failures") or []:
        if isinstance(failure, dict) and failure.get("matched") is not None:
            failure["matched"] = NOT_SHOWN
    return shown


def _call(label: str, stage: str, request_id: str, verdict, seen: list[Exchange]) -> Call:
    exchange = seen[-1] if seen else None
    body = exchange.body if exchange else None
    usable = body is not None and not verdict.unavailable
    findings = [{"rule": f.get("rule"), "check": str(f.get("check")), "action": str(f.get("action") or "review"),
                 "source": f.get("source"), "field": f.get("field"), "message": f.get("message")}
                for f in (body.get("failures") or [] if usable else []) if isinstance(f, dict)]
    return Call(label=label, stage=stage, request_id=request_id,
                status=body.get("status") if usable else None,
                unavailable=(verdict.reasons[0] if verdict.reasons else "guardrail_unavailable") if verdict.unavailable else None,
                rules_version=body.get("rules_version") if body else None,
                checks=[c for c in (body.get("checks") or [] if usable else []) if isinstance(c, dict)],
                findings=findings, notes=[str(n) for n in (body.get("notes") or [] if usable else [])],
                raw=without_matched(body))


def _prompt(seen: list[Exchange]) -> dict | None:
    """The prompt the guardrail returned with a pass or a review: what to send to the model."""
    output = seen[-1].body.get("output") if seen and seen[-1].body else None
    if isinstance(output, dict) and isinstance(output.get("system_instructions"), str) and isinstance(output.get("user_message"), str):
        return {"system_instructions": output["system_instructions"], "user_message": output["user_message"]}
    return None


def _result(calls: list[Call]) -> str:
    """What the GUARDRAIL did, over every call of the test: stopped it, asked for a person, or
    passed it. What the model answered is not scored. A test in which the guardrail could not be
    used is not scored at all: an outage is not a detection."""
    if any(c.unavailable for c in calls):
        return NOT_SCORED
    worst = PASS
    for c in calls:
        if c.stage == "answer":
            failed = [check for check in c.checks if check.get("passed") is False]
            verdict = PASS if not failed else STOPPED if c.status == "out_of_scope" else REVIEW
        else:
            verdict = STOPPED if c.status in ("fail", "out_of_scope") else REVIEW if c.status == "review" else PASS
        if verdict == STOPPED or (verdict == REVIEW and worst == PASS):
            worst = verdict
    return worst


def _finish(test: Case, run_id: str, ids: list[str], calls: list[Call], prompt: dict | None, model: Model | None,
            called: bool, reply: str | None, outcome: Outcome) -> Run:
    return Run(id=run_id, started_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), test=test,
               request_ids=ids, calls=calls, prompt=prompt, model_mode=model.name if model else "none",
               model_called=called, model_reply=reply, outcome=outcome, result=_result(calls),
               policy=next((c.rules_version for c in calls if c.rules_version), None))


def _unavailable_note(verdict) -> list[str]:
    return [f"The guardrail could not be used ({verdict.reasons[0] if verdict.reasons else 'no reason given'}). "
            "The backend treats that as a review, never as a pass."] if verdict.unavailable else []


_MODEL_FAILED = {
    "timeout": "the model did not answer in time", "unreachable": "the model could not be reached",
    "bad_response": "the model's answer could not be read",
}
_REFUSED = {
    "content_filter_prompt": "The provider's content filter refused the prompt. The model was not run.",
    "content_filter_reply": "The provider's content filter cut off the model's reply.",
    "model_refusal": "The model declined to give a reply in the shape asked for.",
}


def run_extraction(test: Case, model: Model | None, schema: dict | None = None, *, only_after_pass: bool = False) -> Run:
    """An inbound email. `schema` is the chosen intent's JSON Schema, from the guardrail's own policy.

    `only_after_pass`: as in run_chat, have the model read the email only when the check before
    it answered `pass`. The backend also has it read an email that is sent for review, and that is
    what happens here when this is left off."""
    run_id = request_id = str(uuid.uuid4())
    attachments = [SimpleNamespace(filename=a.get("name"), text=a.get("text")) for a in test.attachments]
    verdict, seen = call(lambda: gateway.check_document(request_id, subject=test.subject, body_text=test.body,
                                                        attachments=attachments))
    calls = [_call("The email, before any model reads it", "input", request_id, verdict, seen)]
    prompt = _prompt(seen)
    done = lambda outcome, called=False, reply=None: _finish(  # noqa: E731
        test, run_id, [request_id], calls, prompt, model, called, reply, outcome)

    if verdict.blocked:
        return done(Outcome("stopped", KINDS["stopped"], detail=[
            "No model reads this email, and no intent is recorded for it.",
            f"A review task is opened. The reviewer sees: {gateway.review_reason('input', verdict.status, verdict.reasons)}"]))
    notes = _unavailable_note(verdict)
    if verdict.needs_review:
        notes.append(f"A review task is opened. The reviewer sees: {gateway.review_reason('input', verdict.status, verdict.reasons)}")
    kind = "review" if verdict.needs_review else "processed"

    if only_after_pass and verdict.status != "pass":
        return done(Outcome("held_for_review", KINDS["held_for_review"], detail=notes + [
            "The check before the model asked for a review, so nothing is sent to a model. "
            "The backend does have the model read this email."]))
    if prompt is None:
        return done(Outcome(kind, KINDS[kind], detail=notes + [
            "The guardrail returned no prompt, so the testbench has nothing to send to a model and stops here."]))
    if model is None:
        return done(Outcome("prompt_ready", KINDS["prompt_ready"], detail=notes + [
            "Model mode is none, so the test stops here. The prompt the guardrail returned is below."]))

    reply = model.complete(prompt["system_instructions"], prompt["user_message"], json_schema=schema)
    if reply.refused:
        return done(Outcome("provider_refusal", KINDS["provider_refusal"], detail=notes + [_REFUSED.get(reply.refused, reply.refused)]),
                    called=True)
    if reply.error:
        return done(Outcome("model_error", KINDS["model_error"], detail=notes + [
            _MODEL_FAILED.get(reply.error, f"the model answered {reply.error.replace('_', ' ').upper()}") + "."]), called=True)
    try:
        fields = json.loads(reply.text or "")
    except ValueError:
        fields = None
    if not isinstance(fields, dict):
        return done(Outcome("model_error", KINDS["model_error"], detail=notes + [
            "The model's reply was not the JSON object an extraction is. It was not sent to the output check."]),
            called=True, reply=reply.text)

    # Unscored: the backend's confidence comes from token probabilities and its own code (see the top of this file).
    checked, seen = call(lambda: gateway.check_extraction(request_id, intent=test.intent, fields=fields,
                                                          confidences={}, overall_confidence=None))
    calls.append(_call("The extraction, before it is stored", "output", request_id, checked, seen))
    notes += _unavailable_note(checked)
    stored = gateway.without_unsupported_values(fields, checked)
    removed = sorted(name for name in fields if fields[name] is not None and stored.get(name) is None)
    if removed:
        notes.append(f"Not stored, because the guardrail removed the value: {', '.join(removed)}.")
    if checked.needs_review:
        notes.append(f"A review task is opened. The reviewer sees: {gateway.review_reason('output', checked.status, checked.reasons)}")
        kind = "review"
    if checked.status == "fail":
        notes.append("The extraction is kept for the reviewer, and none of it is trusted.")
    return done(Outcome(kind, KINDS[kind], detail=notes or ["The extraction is stored as the model wrote it."]),
                called=True, reply=reply.text)


def run_chat(test: Case, model: Model | None, *, only_after_pass: bool = False) -> Run:
    """A question, with the pasted email body and attachments as the excerpts the model reads.

    `only_after_pass`: ask the model only when both checks before it answered `pass`. The backend
    also asks it after a `review` that it answers with a caution, and that is what happens here
    when this is left off. A caller that must send no flagged text to a model sets it; the run
    then stops where the backend would have gone on, and says what the user would have been shown."""
    run_id = str(uuid.uuid4())
    question_id = f"chat-{run_id}"
    read_id = f"{question_id}:read"
    excerpts = ([{"source_uri": BODY_EXCERPT, "text": test.body}] if test.body.strip() else []) + \
               [{"source_uri": a.get("name") or "", "text": a.get("text") or ""} for a in test.attachments]
    calls: list[Call] = []
    prompt: dict | None = None
    done = lambda outcome, ids, called=False, reply=None: _finish(  # noqa: E731
        test, run_id, ids, calls, prompt, model, called, reply, outcome)

    asked, seen = call(lambda: gateway.check_question(question_id, test.question))
    calls.append(_call("The question alone", "input", question_id, asked, seen))
    before = gateway.decide_question(asked)
    if not before.answer:
        return done(Outcome("fallback", KINDS["fallback"], shown=before.fallback, detail=_unavailable_note(asked) + [
            "The model is not asked."]), [question_id])

    read, seen = call(lambda: gateway.check_question(read_id, test.question, excerpts=excerpts))
    calls.append(_call("The question with the source emails the model reads", "input", read_id, read, seen))
    prompt = _prompt(seen)
    after = gateway.decide_excerpts(read, excerpts)
    if not after.answer:
        return done(Outcome("fallback", KINDS["fallback"], shown=after.fallback, detail=_unavailable_note(read) + [
            "A reply built on these source emails is not shown."]), [question_id, read_id])
    cautions = before.cautions + after.cautions

    held = only_after_pass and (asked.status != "pass" or read.status != "pass")
    if model is None or prompt is None or held:
        why = ("The check before the model asked for a review, so nothing is sent to a model. "
               "The backend does call the model here." if held else
               "The guardrail returned no prompt, so the testbench has nothing to send to a model and stops here."
               if prompt is None else "Model mode is none, so the test stops here. The prompt the guardrail returned is below.")
        would = ("If the reply then passes its own check, the user sees it followed by: " + " ".join(cautions)
                 if cautions else "If the reply then passes its own check, the user sees it as the model wrote it.")
        kind = "held_for_review" if held else "prompt_ready"
        return done(Outcome(kind, KINDS[kind], detail=[why, would]), [question_id, read_id])

    reply = model.complete(prompt["system_instructions"], prompt["user_message"])
    if reply.refused:
        return done(Outcome("provider_refusal", KINDS["provider_refusal"], detail=[_REFUSED.get(reply.refused, reply.refused)]),
                    [question_id, read_id], called=True)
    if reply.error:
        return done(Outcome("model_error", KINDS["model_error"], detail=[
            _MODEL_FAILED.get(reply.error, f"the model answered {reply.error.replace('_', ' ').upper()}") + ".",
            "The backend answers a failed model call with an error, not a reply."]), [question_id, read_id], called=True)

    checked, seen = call(lambda: gateway.check_answer(read_id, reply.text or "", question=test.question))
    calls.append(_call("The model's reply, before anyone sees it", "answer", read_id, checked, seen))
    if checked.answered:
        kind = "answer_with_caution" if cautions else "answer"
        return done(Outcome(kind, KINDS[kind], shown=gateway.with_cautions(checked.answer, cautions)),
                    [question_id, read_id], called=True, reply=reply.text)
    return done(Outcome("fallback", KINDS["fallback"], shown=checked.answer, detail=[
        f"The reply check answered {checked.status}. The model's own reply is not shown."]),
        [question_id, read_id], called=True, reply=reply.text)


def run_test(test: Case, model: Model | None, schema: dict | None = None) -> Run:
    return run_chat(test, model) if test.mode == CHAT else run_extraction(test, model, schema)
