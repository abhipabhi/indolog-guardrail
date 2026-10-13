"""One message, start to finish: the guardrail, then the model, then the guardrail again.

There are two kinds of run, and the intent decides which:

  * `chat`, which is what the screen starts on: a CHAT run. A member of staff asks something. The
    question is the one typed; with none typed, the email itself is read as the question.
  * An extraction intent (the screen offers `rfq`): an EMAIL run. The email is handled as an
    incoming email is, by the extraction pipeline. The question is not used.

The steps themselves are not written here. They are the testbench's two flows
(app/guardrail/flow.py, a copy), which follow the backend:

    chat    1. POST /guardrail/input with the question alone, under a new id `chat-<uuid>`.
            2. POST /guardrail/input with the question and the email, under `chat-<uuid>:read`.
            3. The model is sent the `system_instructions` and `user_message` the guardrail
               returned, and nothing else.
            4. POST /guardrail/answer with the model's reply, under `chat-<uuid>:read`.

    email   1. POST /guardrail/input with the email, its attachment and the fixed task the
               backend sends with every incoming email, under a new id.
            2. The model is sent the prompt the guardrail returned, and nothing else, and is
               asked for the fields of the intent as JSON.
            3. POST /guardrail/output with those fields, under the same id.

What a chat user is shown at the end (the reply, the reply with a caution, or a fallback), and what
the reviewer of an incoming email is told, are decided by the backend's own functions in
app/guardrail/gateway.py.

Five things are this app's own:

  * Every message stands alone. A message is the email, an optional attachment and an optional
    question; nothing from an earlier message is kept for the next, so nothing reaches the model
    that the guardrail has not just checked.
  * The model is called only after a pass, unless CHATBOT_CALL_MODEL_ON_REVIEW=1. The backend also
    calls it after a review: in chat when it answers with a caution, and for every incoming email
    that is sent for review.
  * The guardrail is never skipped. When it cannot be reached the run is "needs review", no model
    is called, and in chat the user is shown the backend's fallback.
  * In an email run the tester chooses the intent. In the backend a model classifies the email
    first; that step is not run here. And as in the testbench, the fields are sent to the output
    check unscored: the backend works out a confidence for each from the model's token
    probabilities.
  * The tester can write a system prompt of their own on the screen. The model is then sent that
    in place of the guardrail's (`EditedSystemPrompt`); the user message and every check stay the
    guardrail's, and the run is marked on the screen and in the log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.guardrail import flow
from app.guardrail.model import Model, ModelReply

ATTACHMENT = "attachment"       # the name the one attachment box is sent under
EMAIL, CHAT = "email", "chat"   # the two kinds of run, as the screen and the log name them

# What the guardrail did, in the words the screen and the log use.
STATUS = {flow.PASS: "pass", flow.REVIEW: "review", flow.STOPPED: "fail", flow.NOT_SCORED: "needs review"}
IN_PLAIN_WORDS = {
    "pass": "The guardrail found nothing in what you sent, so the message goes on as normal. This line is about the "
            "guardrail's check of the message and nothing else: it does not say that the model answered. What the model "
            "replied, and what the user was shown, are above.",
    "review": "The guardrail let it through and asked for a person to look at it.",
    "fail": "The guardrail stopped it.",
    "needs review": "The guardrail could not be reached, so nothing was checked and nothing was sent to a model. "
                    "A person has to look at this one.",
}
# The two one-word replies the guardrail's instructions tell the model to give. Each is the model's own
# doing: the guardrail can have found nothing in the message and the model still decline to answer it.
MODEL_DECLINED = {
    "REFUSE": "REFUSE is the model itself declining: its instructions tell it to reply with this one word to a request "
              "that is not about freight. It is not the guardrail's verdict on the message. The user is shown the "
              "fallback above.",
    "NO_ANSWER": "NO_ANSWER is the model itself saying it cannot answer from the documents it was given, as its "
                 "instructions tell it to. It is not the guardrail's verdict on the message. The user is shown the "
                 "fallback above.",
}
NOT_SHOWN = "(nothing was shown to a user in this run) "
NO_USER = "(an incoming email: nothing is shown to a user) "
# In a system prompt, where the guardrail's fence goes: the line it puts around the documents, new for every message.
MARKER = "{marker}"
_FENCE = re.compile(r"DATA_[0-9a-f]+")


@dataclass(frozen=True)
class Message:
    email: str = ""
    attachment: str = ""
    question: str = ""              # a chat run: left empty, the email is read as the question
    intent: str = ""                # empty: a chat run. Otherwise an email run, as an email of this kind


def mode(message: Message) -> str:
    """The kind of run this message is: EMAIL with an extraction intent, CHAT without one."""
    return EMAIL if message.intent else CHAT


def email_is_question(message: Message) -> bool:
    """A chat run with no question typed: the email is what is asked."""
    return mode(message) == CHAT and not message.question.strip()


def mode_of(run: flow.Run) -> str:
    return EMAIL if run.test.mode == flow.EXTRACTION else CHAT


def run_message(message: Message, model: Model | None, *, call_model_on_review: bool = False,
                schema: dict | None = None) -> flow.Run:
    """Runs one message. `model` None is guardrail-only: the checks before the model, and the
    prompt that would be sent. `schema`: in an email run, the JSON Schema of the intent's fields,
    from the guardrail's own policy."""
    attachments = [{"name": ATTACHMENT, "text": message.attachment}] if message.attachment.strip() else []
    if mode(message) == EMAIL:
        case = flow.Case(mode=flow.EXTRACTION, body=message.email, attachments=attachments, intent=message.intent)
        return flow.run_extraction(case, model, schema, only_after_pass=not call_model_on_review)
    if email_is_question(message):
        # Sent once, as the question. It is not sent again as a source email the model reads.
        case = flow.Case(mode=flow.CHAT, attachments=attachments, question=message.email)
    else:
        case = flow.Case(mode=flow.CHAT, body=message.email, attachments=attachments, question=message.question)
    return flow.run_chat(case, model, only_after_pass=not call_model_on_review)


def edited_prompt(written: str, guardrails: str | None) -> str:
    """The tester's own system prompt, or "" when what is in the box is the guardrail's own or nothing at all."""
    text = (written or "").replace("\r\n", "\n").strip()
    return "" if not text or text == (guardrails or "").strip() else text


def system_prompt_sent(template: str, guardrails: str | None) -> str:
    """A tester's system prompt as it is sent: `{marker}` becomes the fence the guardrail used in its
    own instructions for this message, so the prompt still points at this message's documents."""
    fence = _FENCE.search(guardrails or "")
    return template.replace(MARKER, fence.group(0) if fence else "")


class EditedSystemPrompt:
    """Stands between the flow and the model: the model is sent the tester's system prompt in place
    of the guardrail's. The user message it is sent, and every check, are untouched."""

    def __init__(self, model: Model, template: str) -> None:
        self._model, self._template = model, template
        self.name = model.name
        self.sent: str | None = None            # the system prompt of the last call, as it was sent

    @property
    def answered_by(self) -> str | None:
        return getattr(self._model, "answered_by", None)

    @property
    def tries(self) -> int | None:
        return getattr(self._model, "tries", None)

    def complete(self, system: str, user: str, *, json_schema: dict | None = None) -> ModelReply:
        self.sent = system_prompt_sent(self._template, system)
        return self._model.complete(self.sent, user, json_schema=json_schema)


def status(run: flow.Run) -> str:
    return STATUS[run.result]


def rule_ids(run: flow.Run) -> list[str]:
    """The rules that fired, then any check the model's chat reply did not pass. (What an
    extraction failed is among the rules: the output check names a rule for each field.)"""
    reply_checks = [f"reply check: {check.get('check')}" for call in run.calls if call.stage == "answer"
                    for check in call.checks if check.get("passed") is False]
    return run.rules_fired + reply_checks


def found_in(source: str | None, field: str | None = None, stage: str = "input") -> str:
    """Where a rule fired, in a tester's words: a part of the message, or, when the check was of
    the model's extraction (`stage` "output"), one of its fields."""
    if stage == "output":
        return f"the extraction, field {field}" if field else "the extraction as a whole"
    if source is None:
        return "the message as a whole"
    name = source.removesuffix(" (decoded)")
    # The email is sent as `body` in an email run, and as the excerpt `email:body` in chat.
    part = "the email" if name in (flow.BODY_EXCERPT, "body") else "the question" if name == "question" else f"the {name}"
    return part + (" (after decoding)" if source != name else "")


def hint(run: flow.Run, model_variables: list[str] | None = None, tries: int | None = None) -> str | None:
    """What a tester can do about a run that went wrong for a reason that is not the guardrail's.
    `model_variables`: the setting that names the model of each provider that was asked. `tries`:
    how many times the model was asked for this reply."""
    if run.outcome.kind != "model_error":
        return None
    which = " or ".join(model_variables or ["CHATBOT_GEMINI_MODEL"])
    if any("MODEL NOT FOUND" in line for line in run.outcome.detail):
        return (f"The provider does not offer this model, or not to your key. Set {which} in your settings file "
                "(~/.guardrail-chatbot.env) to a model it does offer, for example gemini-3.5-flash-lite, and start the app again.")
    if any("HTTP 503" in line for line in run.outcome.detail):
        asked = f"{tries} times in a row" if tries and tries > 1 else "when asked"
        return (f"The provider answered that it is busy (HTTP 503) {asked}. That is the provider's fault, not the guardrail's "
                "and not your key's, and it is NOT a result of your test: do not record this run as a pass or a fail. Run it "
                f"again in a minute. If it keeps happening, set {which} in your settings file (~/.guardrail-chatbot.env) to "
                "another model and start the app again. The guardrail's check of the message, below, is still valid.")
    return None


def rule_rows(run: flow.Run) -> list[dict]:
    """The rules that fired, one row for each rule in each place it was found. In chat the
    question is checked twice, alone and then with the email, so a rule found in the question
    comes back twice: that is one finding, and it is listed once."""
    rows = [{"rule id": finding["rule"] or finding["check"], "asks for": finding["action"],
             "found in": found_in(finding["source"], finding["field"], call.stage),
             "what the rule is about": finding["message"]} for call in run.calls for finding in call.findings]
    return list({tuple(row.items()): row for row in rows}.values())


def user_would_see(run: flow.Run) -> str:
    """Chat: the exact text a user is shown, when the run got that far; otherwise why no text was
    shown, and what the product would go on to show. Email: what happens to the email, since no
    user is shown anything."""
    if mode_of(run) == EMAIL:
        return NO_USER + " ".join([run.outcome.title + ".", *run.outcome.detail])
    if run.outcome.shown is not None:
        return run.outcome.shown
    return NOT_SHOWN + " ".join(run.outcome.detail)


def input_text(message: Message) -> str:
    """The email and the attachment as one text, for the log."""
    return message.email + (f"\n\n[{ATTACHMENT}]\n{message.attachment}" if message.attachment.strip() else "")


def log_row(message: Message, run: flow.Run, system_prompt: str = "") -> dict:
    """The run as its row of the run log (app/runlog.py), `notes` left for the tester.
    `system_prompt`: the tester's own, when the model was given one in place of the guardrail's."""
    email = mode_of(run) == EMAIL
    return {"time": run.started_at, "request_id": run.request_ids[0], "mode": mode_of(run),
            "intent": run.test.intent if email else "", "input_text": input_text(message),
            "question": "" if email else run.test.question, "status": status(run), "rule_ids": ", ".join(rule_ids(run)),
            "user_saw": user_would_see(run), "model_reply": run.model_reply or "", "system_prompt": system_prompt,
            "notes": ""}
