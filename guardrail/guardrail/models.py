"""Shapes of the requests, the response and the rules file."""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .input_checks import CHECKS as INPUT_CHECKS
from .output_checks import CHECKS as OUTPUT_CHECKS

Action = Literal["block", "review"]
FailureAction = Literal["block", "review", "refuse"]
# How a filter takes part in a decision:
#   enforce   its findings count
#   shadow    it runs and its findings are recorded, but the decision ignores them
#   off       it does not run
Mode = Literal["enforce", "shadow", "off"]
# The parts of an input request a check can be pointed at.
Part = Literal["question", "body", "attachment"]
PARTS: tuple[str, ...] = ("question", "body", "attachment")
# Where a check looks when the policy does not say. Business risk describes inbound mail, not what
# staff ask; scope is about what is being asked. Everything else reads every part.
DEFAULT_APPLIES_TO: dict[str, tuple[str, ...]] = {"business_risk": ("body", "attachment"), "scope": ("question",),
                                                  "ingest": ("body", "attachment")}
# A whole stage is either enforced or observed. In shadow the stage answers as if every check had
# passed, and records what it would have answered. The output and answer stages can be observed
# as a whole. The input stage cannot: it holds the checks that are enforced in every policy.
StageMode = Literal["enforce", "shadow"]

# The checks no policy may weaken (decided by the owner, 5 Oct 2026). They are what stands between
# a document and the model: the injection rules, the two checks that act on what normalisation
# finds (invisible characters, encoded blocks), and the converter's "this page was not read"
# marker. A policy that puts one of them in shadow, switches it off, leaves it out of `checks`,
# puts its whole stage in shadow, or stops it reading a part it reads by default does not
# validate. So it cannot be stored through the admin API or the command line, and the service
# does not load it from a file. The output `schema` check is not one of them: it is an ordinary
# check and may be in shadow (DECISIONS.md, Q58).
ALWAYS_ENFORCED: dict[str, tuple[str, ...]] = {
    "input": ("ingest", "hidden_characters", "injection_rules", "encoded_content"),
}


# What every policy carries (decided by the owner, 5 Oct 2026). Enforcing a check is not enough if
# a policy can empty it: an enforced `injection_rules` with no rules stops nothing, and a `schema`
# check with no schema for an intent passes anything. So a policy must contain every rule of the
# bundled rule set, by id, and a schema for each of the backend's eight intents; one that does not
# is refused, and the refusal names what is missing. The two lists are the bundled policy's own
# (a test holds them to rules.json), so dropping or renaming a rule is a change here, in code, and
# not something a policy edit can do. A policy may add rules and intents. What a rule's patterns
# say, and what a schema allows, is still the policy's to decide.
REQUIRED_INJECTION_RULES: tuple[str, ...] = (
    "ai_override", "prompt_extraction", "role_hijack", "ai_addressed", "chat_template_tokens",
    "tool_call_syntax", "field_manipulation", "instruction_override", "identity_or_limits",
    "data_exfiltration", "shipment_manipulation", "shipment_manipulation_hindi", "payment_detail_change",
    "payment_redirection", "ai_internals", "override_hindi", "override_hinglish", "override_european",
)
REQUIRED_INTENT_SCHEMAS: tuple[str, ...] = (
    "rfq", "quotation", "shipment_status", "invoice_query", "outstanding", "complaint_shipment", "complaint_billing",
    "potential_sale",
)


def _iso_date(value: Any) -> Any:
    """A review date is written YYYY-MM-DD and nothing else: not a timestamp, not a number."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("a review_by date is written YYYY-MM-DD")
    return date.fromisoformat(value)


ReviewDate = Annotated[date, BeforeValidator(_iso_date)]


def mode_of(config, check: str) -> str:
    """How a check takes part in its stage's decisions: enforce, shadow or off."""
    if check not in config.checks or config.modes.get(check, "enforce") == "off":
        return "off"
    return "shadow" if config.mode == "shadow" else config.modes.get(check, "enforce")


def _always_enforced(stage: str, config) -> None:
    """Refuses a stage that weakens one of the checks in ALWAYS_ENFORCED, and says which and how."""
    held = ALWAYS_ENFORCED.get(stage, ())
    for name in held:
        if name not in config.checks:
            raise ValueError(f"{stage}.checks leaves out {name}, which switches it off; "
                             f"{name} is enforced in every policy")
        if config.modes.get(name, "enforce") != "enforce":
            raise ValueError(f"{stage}.modes sets {name} to {config.modes[name]}; {name} is enforced in every "
                             "policy and cannot be put in shadow or switched off")
        reads = getattr(config, "applies_to", {}).get(name)
        if reads is not None:
            dropped = [part for part in DEFAULT_APPLIES_TO.get(name, PARTS) if part not in reads]
            if dropped:
                raise ValueError(f"{stage}.applies_to stops {name} reading the {', '.join(dropped)}; {name} is "
                                 "enforced in every policy on every part it reads")
    if held and config.mode == "shadow":
        raise ValueError(f"{stage}.mode is shadow, which would put {', '.join(held)} in shadow; "
                         f"they are enforced in every policy, so the {stage} stage cannot be observed as a whole")


def _review_dates(stage: str, config) -> None:
    """Every check in shadow carries the date by which someone decides what becomes of it. Shadow
    without a date is how a check stays watch-only for good with nobody having chosen that."""
    unknown = [name for name in config.review_by if name not in config.checks]
    if unknown:
        raise ValueError(f"review_by given for check(s) not in checks: {', '.join(unknown)}")
    undated = [name for name in config.checks if mode_of(config, name) == "shadow" and name not in config.review_by]
    if undated:
        raise ValueError(f"{stage}: {', '.join(undated)} in shadow with no review_by date; every shadow check "
                         f'carries one, e.g. "review_by": {{"{undated[0]}": "YYYY-MM-DD"}}')


def _modes_for(modes: dict[str, str], checks: list[str]) -> dict[str, str]:
    unknown = [name for name in modes if name not in checks]
    if unknown:
        raise ValueError(f"modes given for check(s) not in checks: {', '.join(unknown)}")
    return modes


def _parts_valid(parts: list[str] | None, what: str) -> None:
    if parts is not None and (not parts or len(set(parts)) != len(parts)):
        raise ValueError(f"{what}: applies_to must name at least one part, each once")


def _timeouts_for(timeouts: dict[str, int], checks: list[str]) -> dict[str, int]:
    unknown = [name for name in timeouts if name not in checks]
    if unknown:
        raise ValueError(f"timeouts given for check(s) not in checks: {', '.join(unknown)}")
    if any(ms <= 0 for ms in timeouts.values()):
        raise ValueError("a timeout must be more than 0 ms")
    return timeouts


# The longest a stage may be given. A policy can shorten or lengthen the budget; it cannot remove it.
_MAX_BUDGET_MS = 120_000


def normalize(label: str) -> str:
    """'RFQ', 'rfq' and 'Complaint-Shipment' / 'complaint_shipment' are treated as the same."""
    return re.sub(r"[\s\-]+", "_", label.strip().lower())


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- requests -------------------------------------------------------------------------------

class Attachment(_Strict):
    name: str = Field(min_length=1)
    text: str = ""                      # extracted or OCR'd text


class InputRequest(_Strict):
    """What's about to go to the model: the request, and the documents (email body, OCR'd attachments)."""
    request_id: str = Field(min_length=1)
    question: str = ""                  # what the model is being asked; empty for plain extraction
    body: str = ""
    attachments: list[Attachment] = Field(default_factory=list)


class FieldIn(_Strict):
    value: Any = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class OutputRequest(_Strict):
    """What the model extracted from an email."""
    request_id: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    fields: dict[str, FieldIn]
    overall_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    possible_injection: bool = False    # set by the model when the email tried to instruct it

    def value(self, name: str) -> Any:
        field = self.fields.get(name)
        return field.value if field else None


# --- response -------------------------------------------------------------------------------

class CheckResult(BaseModel):
    check: str
    passed: bool


class Failure(BaseModel):
    check: str
    action: FailureAction = "block"     # block: stopped; refuse: out of scope; review: through, flagged
    message: str
    field: str | None = None            # output checks: which field
    source: str | None = None           # input checks: "body" or the attachment's name
    rule: str | None = None             # injection rules: which rule matched
    matched: str | None = None          # injection rules: the text that matched


class GuardrailResponse(BaseModel):
    request_id: str
    status: Literal["pass", "review", "fail", "out_of_scope"]   # review: passed on, but a person checks it
    rules_version: str
    checks: list[CheckResult]
    failures: list[Failure]
    output: dict[str, Any] | None               # passed on for pass and review; None otherwise
    message: str | None = None                  # what to show the user when nothing is passed on


class AnswerRequest(_Strict):
    """The model's reply, before anyone sees it."""
    request_id: str = Field(min_length=1)
    answer: str = ""                            # exactly what the model returned; empty if it failed
    question: str = ""                          # optional, for the log


class AnswerResponse(BaseModel):
    request_id: str
    status: Literal["answered", "no_answer", "out_of_scope"]
    answer: str                                 # always something to show the user
    rules_version: str
    checks: list[CheckResult]
    notes: list[str]


# --- rules.json -----------------------------------------------------------------------------

def _known(names: list[str], available: dict) -> list[str]:
    unknown = [n for n in names if n not in available]
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)}; available: {', '.join(available)}")
    if len(set(names)) != len(names):
        raise ValueError("a check is listed twice")
    return names


class RuleExamples(_Strict):
    """A rule's own tests, kept with the rule. A policy whose rule fails its examples is rejected
    when it is loaded, so a rule cannot be saved that no longer does what it says."""
    match: list[str] = Field(min_length=1)
    no_match: list[str] = Field(min_length=1)


# Punctuation that ends the sentence a match sits in, for the `unless` guard.
_SENTENCE_END = re.compile(r"[.;!?\n]")


class InjectionRule(_Strict):
    id: str = Field(min_length=1)
    description: str
    action: Action
    patterns: list[str] = Field(min_length=1)
    # A match is dropped when one of these also matches in the same sentence: "there is no change
    # to our bank details" is not a change of bank details.
    unless: list[str] = Field(default_factory=list)
    # How much this rule counts towards "several warning signs together are an attack".
    weight: float = Field(default=1.0, ge=0.0)
    language: str = "any"                    # the language the patterns are written for
    examples: RuleExamples | None = None
    # The parts this rule reads, if fewer than its check's: a rule about inbound mail has nothing to
    # say about a question staff ask.
    applies_to: list[Part] | None = None
    # The pattern family a business-risk rule belongs to, for reporting firing rates by family
    # (open question Q16): bank_payment, party_amendment, release, pressure.
    family: Literal["bank_payment", "party_amendment", "release", "pressure"] | None = None
    _compiled: list = PrivateAttr(default_factory=list)
    _unless: list = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _compile(self) -> "InjectionRule":
        _parts_valid(self.applies_to, f"rule {self.id}")
        try:
            self._compiled = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in self.patterns]
            self._unless = [re.compile(p, re.IGNORECASE) for p in self.unless]
        except re.error as exc:
            raise ValueError(f"rule {self.id}: bad pattern: {exc}") from exc
        if self.examples:
            from .normalize import plain_view, unmasked_view      # the views a rule is matched against
            for text in self.examples.match:
                if not (self.search(plain_view(text)) or self.search(unmasked_view(text))):
                    raise ValueError(f"rule {self.id}: its own example does not match: {text!r}")
            for text in self.examples.no_match:
                if self.search(plain_view(text)) or self.search(unmasked_view(text)):
                    raise ValueError(f"rule {self.id}: matches an example it must not: {text!r}")
        return self

    @property
    def compiled(self) -> list:
        return self._compiled

    def search(self, text: str, crossing: int | None = None):
        """The first match in `text` that an `unless` pattern does not cancel, or None. With
        `crossing`, only a match that spans that offset counts."""
        for pattern in self._compiled:
            for match in pattern.finditer(text):
                if crossing is not None and not (match.start() < crossing < match.end()):
                    continue
                if self._unless:
                    ends = [m.start() for m in _SENTENCE_END.finditer(text)]
                    start = max((e + 1 for e in ends if e < match.start()), default=0)
                    end = min((e for e in ends if e >= match.end()), default=len(text))
                    if any(u.search(text, start, end) for u in self._unless):
                        continue
                return match
        return None


class HiddenCharacters(_Strict):
    invisible: Action = "review"
    tag_characters: Action = "block"


class EncodedContent(_Strict):
    min_length: int = Field(default=200, ge=40)
    action: Action = "review"


class ScopeRule(_Strict):
    id: str = Field(min_length=1)
    description: str
    patterns: list[str] = Field(min_length=1)
    _compiled: list = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _compile(self) -> "ScopeRule":
        try:
            self._compiled = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in self.patterns]
        except re.error as exc:
            raise ValueError(f"scope rule {self.id}: bad pattern: {exc}") from exc
        return self

    @property
    def compiled(self) -> list:
        return self._compiled


class BusinessRisk(_Strict):
    """Rules for business-email compromise: changed bank details, a new consignee, release without
    the original bill of lading. A match asks for a person; it never blocks, and it does not count
    towards "several warning signs together", because two business requests are not an attack on
    the model."""
    rules: list[InjectionRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _review_only(self) -> "BusinessRisk":
        blocking = [r.id for r in self.rules if r.action != "review"]
        if blocking:
            raise ValueError(f"business_risk rules can only ask for review, not block: {', '.join(blocking)}")
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("two business_risk rules share an id")
        return self


class IngestRules(_Strict):
    """What a converter upstream writes into a document's text when it could not read part of it.
    The PDF converter puts one marker line in place of each scanned page it could not OCR. A
    document carrying a marker goes to a person: the page exists, and neither a model nor this
    guardrail has read it. A rule here matches a converter's own marker and nothing else, so it
    can only ask for review, and it says nothing about the sender."""
    rules: list[InjectionRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _review_only(self) -> "IngestRules":
        wrong = [r.id for r in self.rules if r.action != "review" or not r.id.startswith("ingest.")]
        if wrong:
            raise ValueError(f"an ingest rule asks for review and its id starts with 'ingest.': {', '.join(wrong)}")
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("two ingest rules share an id")
        return self


class ClassifierRules(_Strict):
    """Settings for the injection classifier. Any Hugging Face sequence classifier: name its folder,
    say which of its labels mean "attack", and set the windows and thresholds."""
    model: str = Field(min_length=1)                 # folder under the models directory, or an absolute path
    positive_labels: list[str] = Field(min_length=1)
    window_tokens: int = Field(default=512, ge=16)
    overlap_tokens: int = Field(default=128, ge=0)
    batch_size: int = Field(default=8, ge=1)
    max_windows: int = Field(default=400, ge=1)      # more than this is an error, and so a review
    review_threshold: float = Field(ge=0.0, le=1.0)
    block_threshold: float | None = Field(default=None, ge=0.0, le=1.0)   # None: the classifier never blocks
    escalation_weight: float = Field(default=1.0, ge=0.0)

    @model_validator(mode="after")
    def _consistent(self) -> "ClassifierRules":
        if self.overlap_tokens >= self.window_tokens:
            raise ValueError("overlap_tokens must be smaller than window_tokens")
        if self.block_threshold is not None and self.block_threshold < self.review_threshold:
            raise ValueError("block_threshold must not be below review_threshold")
        return self


class InputRules(_Strict):
    checks: list[str] = Field(min_length=1)
    mode: StageMode = "enforce"
    modes: dict[str, Mode] = Field(default_factory=dict)        # per check; anything not listed is enforced
    # Per check in shadow: the date by which it is reviewed (moved to enforce, kept with a new
    # date, or dropped). Required for every shadow check; /health lists the ones past their date.
    # A date given for a check that is not in shadow is kept and ignored.
    review_by: dict[str, ReviewDate] = Field(default_factory=dict)
    # Shadow checks that run after the answer has been given, so that an observer costs the caller
    # nothing (open question Q23). Only while the check is in shadow: an enforced check decides the
    # answer and always runs before it. If a check named here cannot run (its model is not loaded),
    # it is recorded as `not_observed` and changes nothing, like any shadow check that cannot run.
    after_response: list[str] = Field(default_factory=list)
    # Which parts of the request each check reads (question, body, attachment). Anything not listed
    # reads DEFAULT_APPLIES_TO, or every part.
    applies_to: dict[str, list[Part]] = Field(default_factory=dict)
    # Time allowed for the enforced checks of the whole stage, for all the shadow checks together,
    # and optionally less for one check. Out of time is a review with the reason `<check>.timeout`.
    # The two defaults add up to less than the caller's own 10 s timeout, so the caller gets a
    # reasoned answer from us rather than a timeout of its own, whatever a shadow check does.
    time_budget_ms: int = Field(default=6_000, gt=0, le=_MAX_BUDGET_MS)
    shadow_budget_ms: int = Field(default=2_000, gt=0, le=_MAX_BUDGET_MS)
    timeouts_ms: dict[str, int] = Field(default_factory=dict)
    max_chars: int = Field(default=250_000, gt=0)               # everything together; over it is a review
    # Caps for one part. A question over its cap is stopped; mail over a cap is a review.
    max_part_chars: dict[Literal["question", "body", "attachment"], int] = Field(default_factory=dict)
    hidden_characters: HiddenCharacters = HiddenCharacters()
    encoded_content: EncodedContent = EncodedContent()
    escalate_after: int | None = Field(default=2, ge=2)   # review signals whose weights add up to this = block
    injection_rules: list[InjectionRule] = Field(default_factory=list)
    business_risk: BusinessRisk = BusinessRisk()
    ingest: IngestRules = IngestRules()
    injection_classifier: ClassifierRules | None = None
    ocr_watch_words: list[str] = Field(default_factory=list)   # extra words to recover from OCR misreadings
    scope_rules: list[ScopeRule] = Field(default_factory=list)
    system_instructions: str = Field(min_length=1)

    @field_validator("checks")
    @classmethod
    def _known_checks(cls, names: list[str]) -> list[str]:
        return _known(names, INPUT_CHECKS)

    def parts(self, check: str) -> tuple[str, ...]:
        """The parts of a request this check reads."""
        return tuple(self.applies_to.get(check, DEFAULT_APPLIES_TO.get(check, PARTS)))

    @model_validator(mode="after")
    def _consistent(self) -> "InputRules":
        ids = [r.id for r in self.injection_rules]
        if len(set(ids)) != len(ids):
            raise ValueError("two injection rules share an id")
        absent = [rule for rule in REQUIRED_INJECTION_RULES if rule not in ids]
        if absent:
            raise ValueError(f"input.injection_rules is missing {len(absent)} rule(s) every policy carries: "
                             f"{', '.join(absent)}")
        missing = [p for p in ("{marker}", "REFUSE", "NO_ANSWER") if p not in self.system_instructions]
        if missing:
            raise ValueError(f"system_instructions must contain {', '.join(missing)}")
        _modes_for(self.modes, self.checks)
        _always_enforced("input", self)
        _review_dates("input", self)
        _timeouts_for(self.timeouts_ms, self.checks)
        unknown = [name for name in self.applies_to if name not in self.checks]
        if unknown:
            raise ValueError(f"applies_to given for check(s) not in checks: {', '.join(unknown)}")
        unknown = [name for name in self.after_response if name not in self.checks]
        if unknown:
            raise ValueError(f"after_response names check(s) not in checks: {', '.join(unknown)}")
        for name, parts in self.applies_to.items():
            _parts_valid(parts, name)
        if any(cap <= 0 for cap in self.max_part_chars.values()):
            raise ValueError("a cap in max_part_chars must be more than 0")
        if ("injection_classifier" in self.checks and self.modes.get("injection_classifier", "enforce") != "off"
                and self.injection_classifier is None):
            raise ValueError("injection_classifier is on but has no settings (input.injection_classifier)")
        return self


class IntentRules(_Strict):
    required: list[str] = Field(default_factory=list)
    defaults: dict[str, str] = Field(default_factory=dict)
    allowed_values: dict[str, list[str]] = Field(default_factory=dict)
    numbers: list[str] = Field(default_factory=list)
    min_confidence: dict[str, float] = Field(default_factory=dict)   # per-field overrides
    # The shape the extraction must have, as JSON Schema (draft 2020-12). Taken from the backend's
    # own classes by scripts/export_backend_schemas.py, never written by hand. None: not checked.
    json_schema: dict[str, Any] | None = None
    # The fields that arrive with a confidence score. None: every field must have one.
    scored_fields: list[str] | None = None
    # Fields the grounding check leaves alone: the ones the model is asked to infer, normalise or
    # summarise rather than copy. A name, or "field.leaf" for a key inside a list of objects.
    grounding_exempt: list[str] = Field(default_factory=list)
    # Fields a person must see whenever they carry a value (bank and payment details that reach the ERP).
    high_impact_fields: list[str] = Field(default_factory=list)
    _validator: Any = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _valid_schema(self) -> "IntentRules":
        if self.json_schema is not None:
            try:
                Draft202012Validator.check_schema(self.json_schema)
            except SchemaError as exc:
                raise ValueError(f"json_schema is not a valid JSON Schema: {exc.message}") from None
            self._validator = Draft202012Validator(self.json_schema, format_checker=FormatChecker())
        return self

    def schema_errors(self, instance: dict) -> list:
        """What is wrong with this extraction's shape; empty when it fits or no schema is set."""
        return sorted(self._validator.iter_errors(instance), key=lambda e: list(map(str, e.absolute_path))) \
            if self._validator else []


class OutputRules(_Strict):
    checks: list[str] = Field(min_length=1)
    mode: StageMode = "enforce"
    modes: dict[str, Mode] = Field(default_factory=dict)
    review_by: dict[str, ReviewDate] = Field(default_factory=dict)     # as for the input stage
    time_budget_ms: int = Field(default=6_000, gt=0, le=_MAX_BUDGET_MS)
    shadow_budget_ms: int = Field(default=2_000, gt=0, le=_MAX_BUDGET_MS)
    timeouts_ms: dict[str, int] = Field(default_factory=dict)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_overall_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # What a check's finding does when the check does not say: stop (block, the default) or send to
    # a person (review). Per check, e.g. {"confidence": "review"}.
    actions: dict[str, Action] = Field(default_factory=dict)
    intents: dict[str, IntentRules]

    @field_validator("checks")
    @classmethod
    def _known_checks(cls, names: list[str]) -> list[str]:
        return _known(names, OUTPUT_CHECKS)

    @field_validator("intents")
    @classmethod
    def _normalize_intents(cls, intents: dict[str, IntentRules]) -> dict[str, IntentRules]:
        return {normalize(k): v for k, v in intents.items()}

    @model_validator(mode="after")
    def _known_modes(self) -> "OutputRules":
        _modes_for(self.modes, self.checks)
        _always_enforced("output", self)
        _review_dates("output", self)
        _timeouts_for(self.timeouts_ms, self.checks)
        unknown = [name for name in self.actions if name not in self.checks]
        if unknown:
            raise ValueError(f"actions given for check(s) not in checks: {', '.join(unknown)}")
        absent = [intent for intent in REQUIRED_INTENT_SCHEMAS
                  if intent not in self.intents or self.intents[intent].json_schema is None]
        if absent:
            raise ValueError(f"output.intents has no json_schema for {len(absent)} intent(s) every policy checks: "
                             f"{', '.join(absent)}")
        return self


class Messages(_Strict):
    refusal: str = Field(min_length=1)
    no_answer: str = Field(min_length=1)


ANSWER_CHECKS = ("no_code", "no_leak", "length")                 # what runs when a policy lists nothing
ANSWER_CHECK_NAMES = ANSWER_CHECKS + ("leakage", "grounding", "abstention")   # what a policy may list


class CanaryRules(_Strict):
    """Tokens the model team plants in its system prompt. A reply that contains one has repeated
    its instructions. Off until that team adopts it (proposal P3): there is nothing to look for
    until they plant one."""
    enabled: bool = False
    tokens: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _usable(self) -> "CanaryRules":
        if self.enabled and not self.tokens:
            raise ValueError("the canary check is enabled but no tokens are given")
        if any(len(token.strip()) < 8 for token in self.tokens):
            raise ValueError("a canary token must be at least 8 characters, or ordinary text will match it")
        return self


class LeakageRules(_Strict):
    canary: CanaryRules = CanaryRules()


class AnswerRules(_Strict):
    checks: list[str] = Field(default_factory=lambda: list(ANSWER_CHECKS))
    mode: StageMode = "enforce"
    modes: dict[str, Mode] = Field(default_factory=dict)
    review_by: dict[str, ReviewDate] = Field(default_factory=dict)     # as for the input stage
    max_answer_chars: int = Field(default=1500, gt=0)
    leakage: LeakageRules = LeakageRules()

    @field_validator("checks")
    @classmethod
    def _known_checks(cls, names: list[str]) -> list[str]:
        return _known(names, dict.fromkeys(ANSWER_CHECK_NAMES))

    @model_validator(mode="after")
    def _known_modes(self) -> "AnswerRules":
        _modes_for(self.modes, self.checks)
        _review_dates("answer", self)
        return self


class SourcesRules(_Strict):
    """How long the input check's text is kept for the later checks to read (guardrail/sources.py).
    Long enough to outlast the pipeline's slowest path with its retries; never longer than a week."""
    ttl_seconds: int = Field(default=86_400, gt=0, le=604_800)


class Rules(_Strict):
    """rules.json. Anything misspelled, unknown or malformed is rejected when the file is loaded."""
    version: str = Field(min_length=1)
    updated_by: str | None = None
    updated_at: str | None = None
    messages: Messages
    sources: SourcesRules = SourcesRules()
    input: InputRules
    output: OutputRules
    answer: AnswerRules

    def past_review(self, today: date) -> list[dict[str, str]]:
        """The shadow checks whose review_by date has gone by: the date itself is the last day."""
        return [{"stage": stage, "check": name, "review_by": config.review_by[name].isoformat()}
                for stage, config in (("input", self.input), ("output", self.output), ("answer", self.answer))
                for name in config.checks
                if mode_of(config, name) == "shadow" and config.review_by[name] < today]
