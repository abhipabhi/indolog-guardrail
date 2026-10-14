"""The guardrail itself: run the checks in order, pass the output on or stop it.

Three rules live here, in code, where no policy setting can reach them:

  * FAIL CLOSED. An ENFORCED check that cannot run (it raises, it runs out of time, its model or
    its store is missing) sends the request to a person. It never passes it, and it never becomes
    an HTTP 500.
  * NO DOCUMENT TEXT IN THE LOG. A decision is logged as ids, offsets, hashes and verdicts.
  * SHADOW CHANGES NOTHING. A check, or a whole stage, in shadow mode runs and is recorded. What
    it finds does not change the answer, and neither does its failing to run: that is recorded as
    `not_observed` with the reason (`<check>.error`, `.timeout`, `.unavailable`, `.queue_full`) and
    the request goes on as if the check were not there (the owner's correction of 3 Oct 2026). A
    check that runs after the response is a shadow check by definition; an enforced check always
    runs before the answer.
  * EVERY DECISION IS WRITTEN DOWN WITH ITS NUMBERS. One JSON line per decision on the logger
    `guardrail.decisions`: each check's mode, verdict, score and the measures it works from, so a
    threshold can be replayed offline against real traffic. Numbers and ids, never text.
  * OUT OF TIME IS A REVIEW. Each stage has a time budget. An enforced check that cannot finish
    inside it sends the request to a person with the reason `<check>.timeout`. A policy can change
    the budget; it cannot remove it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import deadline, leakage, sources
from .answer_checks import CHECKS as ANSWER_CHECKS
from .answer_checks import SOURCE_CHECKS as ANSWER_SOURCE_CHECKS
from .answer_checks import refused_in_scope
from .input_checks import CHECKS as INPUT_CHECKS
from .input_checks import READINESS as INPUT_READINESS
from .input_checks import wrap
from .normalize import plain_view
from .models import (AnswerRequest, AnswerResponse, FieldIn, GuardrailResponse, InputRequest, OutputRequest, Rules,
                     mode_of, normalize)
from .output_checks import CHECKS as OUTPUT_CHECKS
from .policy import JsonPolicyStore as RulesFile  # noqa: F401  (the name callers and tests have always used)

log = logging.getLogger("guardrail")

# How bad each action is, for picking a filter's verdict and its score when it gives none.
_SEVERITY = {"review": 0.5, "refuse": 1.0, "block": 1.0}
_VERDICT = {"review": "review", "refuse": "refuse", "block": "block"}
_NO_LIMIT = 10 ** 9     # ms; a check with no timeout of its own is limited by its stage's budget


@dataclass
class FilterOutcome:
    """What one check did on one request. This is what the audit trail and the traces are built from."""

    filter_id: str
    stage: str                      # input | output | answer
    mode: str                       # enforce | shadow
    verdict: str                    # allow | review | block | refuse | not_observed (a shadow check that could not run)
    score: float | None             # 0..1; the filter's own score when it has one
    reason_code: str | None         # the rule or check that decided the verdict
    latency_ms: float
    error: bool = False
    findings: list[dict] = field(default_factory=list, repr=False)
    # The numbers the check works from (a size, a score, a lowest confidence), so that a different
    # threshold can be tried against recorded traffic. Numbers only.
    measures: dict[str, float] = field(default_factory=dict)
    started_ns: int = 0             # when the check began, on the wall clock; for traces

    @property
    def rules(self) -> list[str]:
        """The id of every rule that fired in this check, in the order found, each once.
        `reason_code` is the worst of them; this is all of them. Ids only: a rule's id comes from
        the policy or the code, never from the document."""
        return list(dict.fromkeys(f.get("rule") or f["check"] for f in self.findings))


@dataclass
class Decision:
    """One stage's answer for one request, with how it was reached.

    `status` is what the caller was told. `would_be` is what it would have been told with every
    shadow check enforced; the two differ only when something is in shadow mode.
    """

    stage: str
    request_id: str
    status: str
    would_be: str
    rules_version: str
    outcomes: list[FilterOutcome]
    measures: dict[str, float] = field(default_factory=dict)       # stage-level numbers, e.g. the weight of warning signs


# Called with every Decision: the audit sink, the tracer, the evaluation harness. An observer that
# raises is ignored; observing must never change or delay an answer.
OBSERVERS: list[Callable[[Decision], None]] = []


decisions = logging.getLogger("guardrail.decisions")


def _record(decision: Decision) -> None:
    """The decision as one JSON line: what a shadow rollout is replayed from."""
    decisions.info(json.dumps({
        "event": "guardrail.decision", "stage": decision.stage, "request_id": decision.request_id,
        "status": decision.status, "would_be": decision.would_be, "policy_version": decision.rules_version,
        "measures": decision.measures,
        "checks": [{"id": o.filter_id, "mode": o.mode, "verdict": o.verdict, "reason": o.reason_code,
                    "rules": o.rules, "score": None if o.score is None else round(o.score, 6),
                    "ms": round(o.latency_ms, 2), "error": o.error, "measures": o.measures}
                   for o in decision.outcomes],
    }, sort_keys=True))


def _notify(decision: Decision) -> None:
    try:
        _record(decision)
    except Exception as exc:        # noqa: BLE001 -- writing it down must never change an answer
        log.error("a decision could not be recorded: %s", type(exc).__name__)
    for observer in list(OBSERVERS):
        try:
            observer(decision)
        except Exception as exc:    # noqa: BLE001
            log.error("a decision observer raised %s", type(exc).__name__)


# --- logging ---------------------------------------------------------------------------------------

def _describe(failure: dict, refs: dict[str, str]) -> str:
    """One finding as it goes in the log: ids, offsets and a hash. Never the message, the matched
    text, an extracted value or an attachment's filename, all of which come from the document."""
    text = failure.get("rule") or failure["check"]
    if failure.get("field"):
        text += f":{failure['field']}"
    source = failure.get("source")
    if source:
        base, _, decoded = source.partition(" (decoded)")
        text += "@" + refs.get(base, "attachment") + ("/decoded" if source != base else "")
    if failure.get("span"):
        text += "[{}:{}]/{}#{}".format(*failure["span"], failure["view"], failure["matched_hash"])
    return f"{text}={failure['action']}"


def _source_refs(request) -> dict[str, str]:
    refs = {"question": "question", "body": "body"}
    for i, attachment in enumerate(getattr(request, "attachments", [])):
        refs.setdefault(attachment.name, f"attachment[{i}]")
    return refs


def _log(stage: str, request, rules: Rules, status: str, would_be: str, failures: list, shadow: list) -> None:
    refs = _source_refs(request)
    line = "; ".join(_describe(f, refs) for f in failures) or "all checks passed"
    if shadow or would_be != status:
        line += f"  | shadow, would be {would_be.upper()}: " + ("; ".join(_describe(f, refs) for f in shadow) or "-")
    log.info("%-6s %s  %s  rules=%s  %s", stage, request.request_id, status.upper(), rules.version, line)


# --- running a stage's checks ----------------------------------------------------------------------

def _status(failures: list) -> str:
    actions = {f["action"] for f in failures}
    return ("fail" if "block" in actions else "out_of_scope" if "refuse" in actions
            else "review" if "review" in actions else "pass")


def _outcome(name: str, stage: str, mode: str, found: list, started: float, error: bool) -> FilterOutcome:
    worst = max(found, key=lambda f: _SEVERITY[f["action"]], default=None)
    scores = [f["score"] if f.get("score") is not None else _SEVERITY[f["action"]] for f in found]
    return FilterOutcome(
        filter_id=name, stage=stage, mode=mode,
        verdict=_VERDICT[worst["action"]] if worst else "allow",
        score=max(scores) if scores else 0.0,
        reason_code=(worst.get("rule") or worst["check"]) if worst else None,
        latency_ms=(elapsed := time.perf_counter() - started) * 1000, error=error, findings=found,
        started_ns=time.time_ns() - int(elapsed * 1e9))


# What a check raises when the thing it needs is not there, as against going wrong while it works:
# a model folder that is missing, a runtime that is not installed, no source store.
_UNAVAILABLE = (FileNotFoundError, ImportError, sources.NoStore)


def _why(exc: Exception) -> str:
    return "timeout" if isinstance(exc, deadline.TimedOut) else "unavailable" if isinstance(exc, _UNAVAILABLE) else "error"


def _not_observed(name: str, stage: str, why: str, started: float | None = None) -> FilterOutcome:
    """A shadow check that could not run. It is written down with the reason, and nothing else
    follows from it: no finding, no review, no change to what the caller is told."""
    elapsed = time.perf_counter() - started if started is not None else 0.0
    return FilterOutcome(name, stage, "shadow", "not_observed", None, f"{name}.{why}", elapsed * 1000, error=True,
                         started_ns=time.time_ns() - int(elapsed * 1e9))


def _run_check(name: str, stage: str, mode: str, config, run, ends: float) -> tuple[FilterOutcome, list, bool]:
    """Runs one check against its deadline. Returns (what it did, its findings, whether it could
    not run). A check never raises out of here. One that cannot run is, when enforced, a review
    finding; in shadow it is `not_observed` and has no findings."""
    started, error = time.perf_counter(), False
    noted: list[dict] = []
    try:
        with deadline.until(ends):
            deadline.check()        # the budget may already be spent before this check starts
            default = getattr(config, "actions", {}).get(name, "block")
            found = [{"check": name, "action": default, **f} for f in run()]
        # An "allow" entry is a note, not a finding: a score for a request the check passed.
        noted = [f for f in found if f["action"] == "allow"]
        found = [f for f in found if f["action"] != "allow"]
    except Exception as exc:
        # Only the exception's type is logged: its message can quote the document.
        why = _why(exc)
        if mode == "shadow":
            log.warning("shadow check %s could not run (%s, %s); not observed", name, why, type(exc).__name__)
            return _not_observed(name, stage, why, started), [], True
        # Fail closed: an enforced check that can't run sends the request to a person. It never
        # passes it, and it never becomes an HTTP 500.
        error = True
        if why == "timeout":
            log.error("check %s ran out of time; sent to review", name)
            found = [{"check": name, "action": "review", "rule": f"{name}.timeout", "error": True,
                      "message": f"the {name} check ran out of time, so a person should look at this"}]
        else:
            log.error("check %s raised %s; sent to review", name, type(exc).__name__)
            found = [{"check": name, "action": "review", "rule": f"{name}.error", "error": True,
                      "message": f"the {name} check could not run, so a person should look at this"}]
    outcome = _outcome(name, stage, mode, found, started, error)
    outcome.score = max([outcome.score or 0.0] + [f["score"] for f in noted if f.get("score") is not None])
    outcome.measures = {key: value for f in noted for key, value in f.get("measures", {}).items()}
    return outcome, found, error


def _run(stage: str, config, registry, *args, stop_on_block: bool = False, readiness: dict | None = None):
    """Runs a stage's checks in order. Returns (checks, failures, shadow failures, outcomes, later).

    `failures` are the findings that count. `shadow failures` come from checks in shadow mode (or
    from every check, when the stage itself is in shadow) and are recorded but never decide. Only
    the output stage can be in shadow as a whole here: the input stage holds the checks every
    policy enforces, and no policy that shadows it loads.

    A shadow check that cannot run is in `outcomes` as `not_observed` and nowhere else.

    `later` names the shadow checks the policy puts after the response (`after_response`). Such a
    check is not run here. What is checked here is that it CAN run (its entry in `readiness`): if
    it can, the caller runs it once the answer has gone; if it cannot, it is `not_observed` now and
    is not run later. Only a shadow check is ever deferred: an enforced check named in
    `after_response` runs here, before the answer, like any other.
    """
    checks, failures, shadow, outcomes, later = [], [], [], [], []
    stopped = False
    budget_ends = time.monotonic() + config.time_budget_ms / 1000
    # What the shadow checks may use between them. With the whole stage in shadow nothing is
    # enforced, so they have the stage's budget as well.
    shadow_left = (config.shadow_budget_ms + (config.time_budget_ms if config.mode == "shadow" else 0)) / 1000
    deferred = set(getattr(config, "after_response", ()))
    for name in config.checks:
        mode = config.modes.get(name, "enforce")
        if mode == "off":
            continue
        if config.mode == "shadow":
            mode = "shadow"
        if stopped and mode == "enforce":
            continue        # e.g. an oversized email isn't scanned any further
        # An enforced check has what is left of the stage's budget, or its own timeout if that is
        # less. A shadow check draws on the shadow budget instead, and the time it takes is handed
        # back to the enforced checks afterwards: a slow observer must not be the reason an
        # enforced check runs out of time, or the reason the caller gives up waiting.
        own = config.timeouts_ms.get(name, _NO_LIMIT) / 1000
        began = time.monotonic()
        ends = began + min(own, shadow_left) if mode == "shadow" else min(budget_ends, began + own)
        after = mode == "shadow" and name in deferred
        if after:
            ready = (readiness or {}).get(name)
            outcome, found, error = _run_check(name, stage, mode, config,
                                               (lambda: ready(*args) or []) if ready else (lambda: []), ends)
            if not error:
                later.append(name)      # it can run: it will, once the answer has gone
                spent = time.monotonic() - began
                budget_ends, shadow_left = budget_ends + spent, max(0.0, shadow_left - spent)
                continue
        else:
            outcome, found, error = _run_check(name, stage, mode, config, lambda: registry[name](*args), ends)
        outcomes.append(outcome)
        if mode == "shadow":
            spent = time.monotonic() - began
            budget_ends += spent
            shadow_left = max(0.0, shadow_left - spent)
            shadow += found         # nothing, if it could not run: that is in its outcome only
            continue
        checks.append({"check": name, "passed": not found})
        failures += found
        if stop_on_block and any(f["action"] in ("block", "refuse") for f in found):
            stopped = True
    return checks, failures, shadow, outcomes, later


def _escalation(config, failures: list) -> list[dict]:
    """Several weak signals together are treated as an attack: e.g. an override plus a request for
    customer data, or hidden characters plus anything else. A check that failed to run is a reason
    for review, not a warning sign about the email, so it does not count."""
    weights: dict[str, float] = {}
    for f in failures:
        if f["action"] == "review" and not f.get("error") and f.get("escalates", True):
            name = f.get("rule") or f["check"]
            weights[name] = max(weights.get(name, 0.0), f.get("weight", 1.0))
    signals = sorted(name for name, weight in weights.items() if weight > 0)
    if config.escalate_after and sum(weights.values()) >= config.escalate_after:
        return [{"check": "escalation", "action": "block",
                 "message": f"{len(signals)} warning signs together: {', '.join(signals)}"}]
    return []


def _review_weight(findings: list) -> float:
    """What "several warning signs together" adds up to: the number escalate_after is held against."""
    weights: dict[str, float] = {}
    for f in findings:
        if f["action"] == "review" and not f.get("error") and f.get("escalates", True):
            name = f.get("rule") or f["check"]
            weights[name] = max(weights.get(name, 0.0), f.get("weight", 1.0))
    return sum(weights.values())


def _conclude(stage: str, request, rules: Rules, failures: list, shadow: list, outcomes: list,
              would_be_failures: list) -> None:
    """Writes the decision down: the log line, the JSON record, the observers."""
    status, would_be = _status(failures), _status(would_be_failures)
    _log(stage, request, rules, status, would_be, failures, shadow)
    measures = {"review_weight": _review_weight(would_be_failures)} if stage == "input" else {}
    _notify(Decision(stage, request.request_id, status, would_be, rules.version, outcomes, measures))


def _respond(request, rules: Rules, checks: list, failures: list, output) -> GuardrailResponse:
    status = _status(failures)
    stopped = status in ("fail", "out_of_scope")
    return GuardrailResponse(request_id=request.request_id, status=status, rules_version=rules.version,
                             checks=checks, failures=failures, output=None if stopped else output(),
                             message=rules.messages.refusal if stopped else None)


def _finish(stage: str, request, rules: Rules, checks: list, failures: list, shadow: list, outcomes: list,
            would_be_failures: list, output) -> GuardrailResponse:
    _conclude(stage, request, rules, failures, shadow, outcomes, would_be_failures)
    return _respond(request, rules, checks, failures, output)


def _source_readers(rules: Rules) -> list[str]:
    """The modes of the later checks that read what the input check saw. None: nothing is stored."""
    found = []
    for config, name in ((rules.output, "grounding"), (rules.answer, "grounding"), (rules.answer, "abstention")):
        mode = mode_of(config, name)
        if mode != "off":
            found.append(mode)
    return found


def _keeps_sources(rules: Rules) -> bool:
    return bool(_source_readers(rules))


def _remember(request: InputRequest, rules: Rules, store, status: str, findings: list) -> str | None:
    """Stores what the input check read, for the output check of the same request. Returns what the
    store said (sources.STORED, SAME or CONFLICT), or None if it failed. A store that fails is logged
    by the kind of error only; the output check then finds no record and asks for review, which is
    the fail-closed outcome."""
    parts = [("question", request.question), ("body", request.body)] + [(a.name, a.text) for a in request.attachments]
    record = sources.InputRecord(
        request_id=request.request_id, parts=tuple((name, plain_view(text)) for name, text in parts if text),
        status=status, reasons=tuple(sorted({f.get("rule") or f["check"] for f in findings})),
        rules_version=rules.version)
    try:
        return store.put(record, rules.sources.ttl_seconds)
    except Exception as exc:
        log.error("the source store raised %s; the input for %s was not kept", type(exc).__name__, request.request_id)
        return None


_SOURCE_CONFLICT = {"check": "sources", "action": "review", "rule": "source_conflict", "escalates": False,
                    "message": "a different input was already checked under this request id; the first one stands"}


def run_input(request: InputRequest, rules: Rules, store=None, defer=None) -> GuardrailResponse:
    """Before the model. On pass or review, the output is what to send to the model. Otherwise the
    model isn't called and `message` is what to show the user.

    `defer`, when given, is called with a function to run once the answer has gone (the service
    passes its after-response worker, guardrail/after.py). The shadow checks the policy puts after
    the response run there, and the decision is written down when they finish. Without it they run
    here, at the end: the same decision, later for the caller."""
    config = rules.input
    checks, failures, shadow, outcomes, later = _run("input", config, INPUT_CHECKS, request, config,
                                                     stop_on_block=True, readiness=INPUT_READINESS)
    failures += _escalation(config, failures)
    would_be = failures + shadow
    would_be = would_be + [e for e in _escalation(config, would_be) if not any(f["check"] == "escalation" for f in failures)]
    readers = _source_readers(rules) if store is not None else []
    if readers and _remember(request, rules, store, _status(failures), would_be) == sources.CONFLICT:
        # The same request id with different text: a retry that is not a retry, or someone reusing
        # an id. A person looks. It counts only if a check that reads the store is enforced.
        log.warning("input %s: source_conflict", request.request_id)
        if "enforce" in readers:
            failures = failures + [dict(_SOURCE_CONFLICT)]
            checks = checks + [{"check": "sources", "passed": False}]
        else:
            shadow = shadow + [dict(_SOURCE_CONFLICT)]
        would_be = would_be + [dict(_SOURCE_CONFLICT)]
    if not later:
        return _finish("input", request, rules, checks, failures, shadow, outcomes, would_be,
                       lambda: wrap(request, config.system_instructions))

    def complete(observe: bool = True) -> None:
        """The checks that run after the response, and then the record of the whole decision. What
        they find is recorded as what the answer would have been. One that fails here is recorded
        as `not_observed`, as a shadow check that fails anywhere is. With `observe` False (the
        worker had no room) the checks are not run and the record says so (`<check>.queue_full`)."""
        seen, observed = list(shadow), list(would_be)
        left = config.shadow_budget_ms / 1000
        for name in later:
            if not observe:
                outcomes.append(_not_observed(name, "input", "queue_full"))
                continue
            began = time.monotonic()
            own = config.timeouts_ms.get(name, _NO_LIMIT) / 1000
            outcome, found, error = _run_check(name, "input", "shadow", config,
                                               lambda name=name: INPUT_CHECKS[name](request, config), began + min(own, left))
            left = max(0.0, left - (time.monotonic() - began))
            outcomes.append(outcome)
            seen += found
            observed += found
        observed += [e for e in _escalation(config, observed) if not any(f["check"] == "escalation" for f in observed)]
        _conclude("input", request, rules, failures, seen, outcomes, observed)

    response = _respond(request, rules, checks, failures, lambda: wrap(request, config.system_instructions))
    if defer is None:
        complete()
    else:
        defer(complete)
    return response


def run_output(request: OutputRequest, rules: Rules, store=None) -> GuardrailResponse:
    """After the model has extracted the fields. On pass or review, the output is the validated data,
    with any value a check removed set to null."""
    with sources.using(store):
        return _run_output(request, rules)


def _run_output(request: OutputRequest, rules: Rules) -> GuardrailResponse:
    config = rules.output
    intent_rules = config.intents.get(normalize(request.intent))
    submitted = request
    if intent_rules is None:
        unknown = [{"check": "intent", "action": "block",
                    "message": f"no rules for intent '{request.intent}'; known: {', '.join(config.intents)}"}]
        enforced = [] if config.mode == "shadow" else unknown
        response = _finish("output", request, rules, [], enforced, unknown if config.mode == "shadow" else [], [],
                           unknown, lambda: {name: f.value for name, f in submitted.fields.items()})
        return response.model_copy(update={"message": None})

    # Missing fields that have a default get it before the checks run.
    fields = dict(request.fields)
    for name, default in intent_rules.defaults.items():
        if request.value(name) is None or str(request.value(name)).strip() == "":
            fields[name] = FieldIn(value=default, confidence=1.0)
    request = request.model_copy(update={"fields": fields})

    checks, failures, shadow, outcomes, _ = _run("output", config, OUTPUT_CHECKS, request, intent_rules, config)
    # A stage in shadow hands back exactly what it was given: no defaults filled in, nothing removed.
    returned = submitted if config.mode == "shadow" else request
    # A value an enforced check could not stand behind goes back as null. Removing is the only
    # edit the guardrail makes to an extraction.
    removed = set() if config.mode == "shadow" else {f["field"] for f in failures if f.get("remove")}
    response = _finish("output", request, rules, checks, failures, shadow, outcomes, failures + shadow,
                       lambda: {name: None if name in removed else f.value for name, f in returned.fields.items()})
    return response.model_copy(update={"message": None})   # extraction results aren't shown to a user


_SENTINEL = re.compile(r"^\W*(REFUSE|NO_ANSWER)\W*$", re.IGNORECASE)


def run_answer(request: AnswerRequest, rules: Rules, store=None) -> AnswerResponse:
    """After the model. Always returns an answer to show: the checked reply, the refusal, or
    "I can't answer that"."""
    with sources.using(store):
        return _run_answer(request, rules)


def _run_answer(request: AnswerRequest, rules: Rules) -> AnswerResponse:
    text, checks, notes = request.answer.strip(), [], []
    config = rules.answer
    outcomes: list[FilterOutcome] = []

    def done(status: str, answer: str) -> AnswerResponse:
        # Check names only. The notes are for the caller; keeping them out of the log means a note
        # that one day quotes the reply can't leak it.
        not_passed = [c["check"] for c in checks if not c["passed"]]
        line = "; ".join(not_passed) or ("model_signal" if status != "answered" else "all checks passed")
        shown_status, shown_answer, shown_checks, shown_notes = status, answer, checks, notes
        if config.mode == "shadow":
            # Observed, not enforced: the caller gets the model's reply exactly as it was.
            line += f"  | shadow, would be {status.upper()}"
            shown_status, shown_answer, shown_checks, shown_notes = "answered", request.answer, [], []
        log.info("answer %s  %s  rules=%s  %s", request.request_id, shown_status.upper(), rules.version, line)
        _notify(Decision("answer", request.request_id, shown_status, status, rules.version, outcomes))
        return AnswerResponse(request_id=request.request_id, status=shown_status, answer=shown_answer,
                              rules_version=rules.version, checks=shown_checks, notes=shown_notes)

    # The model's own signals: REFUSE for out of scope, NO_ANSWER when it can't answer.
    sentinel = _SENTINEL.match(text)
    if not text or (sentinel and sentinel.group(1).upper() == "NO_ANSWER") or "NO_ANSWER" in text:
        checks.append({"check": "model_signal", "passed": True})
        notes.append("empty reply" if not text else "the model said it can't answer")
        return done("no_answer", rules.messages.no_answer)
    if sentinel or re.search(r"\bREFUSE\b", text):
        checks.append({"check": "model_signal", "passed": True})
        notes.append("the model refused: out of scope")
        mode = config.modes.get("abstention", "enforce") if "abstention" in config.checks else "off"
        if mode != "off":
            # The user is shown the refusal either way. This only records that the model turned
            # away a question the input check had found in scope.
            started = time.perf_counter()
            try:
                flagged = refused_in_scope(request.request_id)
                found = [{"check": "abstention", "action": "review", "rule": "abstention.refused_in_scope"}] if flagged else []
                outcomes.append(_outcome("abstention", "answer", mode, found, started, False))
                if flagged and mode == "enforce":
                    notes.append("abstention.refused_in_scope: the input check found this question in scope")
            except Exception as exc:
                if mode == "shadow" or config.mode == "shadow":
                    log.warning("shadow check abstention could not run (%s, %s); not observed", _why(exc), type(exc).__name__)
                    outcomes.append(_not_observed("abstention", "answer", _why(exc), started))
                else:
                    log.error("check abstention raised %s", type(exc).__name__)
                    outcomes.append(_outcome("abstention", "answer", mode,
                                             [{"check": "abstention", "action": "review", "rule": "abstention.error"}],
                                             started, True))
        return done("out_of_scope", rules.messages.refusal)

    for name in config.checks:
        if name in ("length", "abstention") or config.modes.get(name, "enforce") == "off":
            continue
        shadowed = config.modes.get(name) == "shadow"
        started = time.perf_counter()
        try:
            if name == "leakage":
                # Secrets, card numbers and Aadhaar numbers are masked in the reply, which is then
                # shown: this endpoint already hands back a reply it has changed (a shortened one).
                # So is a number given after an account, Aadhaar or IFSC label, whatever it is (Q69).
                # A planted canary token is different: the reply has repeated the model's
                # instructions, and is replaced like any other leak of them.
                canary = config.leakage.canary
                leaked = canary.enabled and any(token in text for token in canary.tokens)
                masked, kinds = leakage.mask(text, labelled=True)
                found = ([{"check": name, "action": "block", "rule": "leakage.canary"}] if leaked else []) + \
                        [{"check": name, "action": "review", "rule": f"leakage.{kind}"} for kind in dict.fromkeys(kinds)]
                outcome = _outcome(name, "answer", "shadow" if shadowed else "enforce", found, started, False)
                outcome.measures = {"found": float(len(kinds)), "canary": float(leaked)}
                outcomes.append(outcome)
                if shadowed:
                    continue
                checks.append({"check": name, "passed": not found})
                if leaked:
                    notes.append("leakage.canary: the reply repeated the model's instructions; replaced with the refusal")
                    return done("out_of_scope", rules.messages.refusal)
                if kinds:
                    text = masked
                    counted = ", ".join(f"{kinds.count(kind)} {kind}" for kind in dict.fromkeys(kinds))
                    notes.append(f"leakage: masked {counted}")
                continue
            if name in ANSWER_SOURCE_CHECKS:
                # A check against the stored input. What it cannot support is withheld, not refused.
                unsupported = ANSWER_SOURCE_CHECKS[name](text, rules, request.request_id)
                found = [{"check": name, "action": "review", "rule": unsupported[0]}] if unsupported else []
                outcomes.append(_outcome(name, "answer", "shadow" if shadowed else "enforce", found, started, False))
                if shadowed:
                    continue
                checks.append({"check": name, "passed": unsupported is None})
                if unsupported:
                    notes.append(f"{unsupported[0]}: {unsupported[1]}; the reply was withheld")
                    return done("no_answer", rules.messages.no_answer)
                continue
            problem = ANSWER_CHECKS[name](text, rules)
        except Exception as exc:
            if shadowed or config.mode == "shadow":
                # Shadow changes nothing, and that includes failing to run.
                log.warning("shadow check %s could not run (%s, %s); not observed", name, _why(exc), type(exc).__name__)
                outcomes.append(_not_observed(name, "answer", _why(exc), started))
                continue
            # Fail closed: a reply an enforced check could not look at is never shown. This endpoint
            # has no review status, so the user gets "can't answer" and the reason goes in the notes.
            log.error("check %s raised %s; reply withheld", name, type(exc).__name__)
            found = [{"check": name, "action": "review", "rule": f"{name}.error"}]
            outcomes.append(_outcome(name, "answer", "enforce", found, started, True))
            checks.append({"check": name, "passed": False})
            notes.append(f"{name}.error: the check could not run, so the reply was withheld")
            return done("no_answer", rules.messages.no_answer)
        found = [{"check": name, "action": "block", "rule": name}] if problem else []
        outcomes.append(_outcome(name, "answer", "shadow" if shadowed else "enforce", found, started, False))
        if shadowed:
            continue
        checks.append({"check": name, "passed": problem is None})
        if problem:
            notes.append(f"{problem}; replaced with the refusal")
            return done("out_of_scope", rules.messages.refusal)

    length_mode = config.modes.get("length", "enforce") if "length" in config.checks else "off"
    if length_mode != "off":
        too_long = [{"check": "length", "action": "review", "rule": "length"}] if len(text) > config.max_answer_chars else []
        outcome = _outcome("length", "answer", length_mode, too_long, time.perf_counter(), False)
        outcome.measures = {"reply_chars": float(len(text))}
        outcomes.append(outcome)
    if length_mode == "enforce":
        limit = config.max_answer_chars
        checks.append({"check": "length", "passed": len(text) <= limit})
        if len(text) > limit:
            cut = text[:limit]
            text = cut[: cut.rfind(". ") + 1] if ". " in cut else cut + "…"
            notes.append("the reply was shortened")
    return done("answered", text)
