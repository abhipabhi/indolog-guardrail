"""The guardrail as an API. It never calls a model: it sits before and after the one you have.

    uvicorn guardrail.app:app

Then open http://localhost:8000/docs to try it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Body, FastAPI

from . import classifier, db, engine, tracing
from .after import AfterResponse
from .examples import ANSWER_EXAMPLES, INPUT_EXAMPLES, OUTPUT_EXAMPLES
from .engine import run_answer, run_input, run_output
from .models import AnswerRequest, AnswerResponse, GuardrailResponse, InputRequest, OutputRequest
from .sources import MemorySourceStore
from .policy import store_from_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("guardrail")

# Without a database: policy from the file, what each input check saw kept in this process's
# memory (right for one replica), decisions in the log only.
RULES = store_from_env()
SOURCES = MemorySourceStore()
DATABASE = None
# Shadow checks the policy puts after the response run here, one at a time, once the answer has gone.
AFTER = AfterResponse()


def _use_database() -> None:
    """With a database: the policy is the active version there (the file is what is used until one
    has been read, and whenever the database cannot be), what each input check saw is shared by
    every replica, every decision is a row, and the admin API is mounted."""
    global RULES, SOURCES, DATABASE
    import threading

    import time

    from . import decisions_db
    from .decisions_db import DecisionWriter
    from .policy_db import PostgresPolicyStore
    from .sources_db import PostgresSourceStore

    DATABASE = db.engine_from_env()
    RULES = PostgresPolicyStore(DATABASE, fallback=RULES, prepare=classifier.prepare)
    SOURCES = PostgresSourceStore(DATABASE)
    engine.OBSERVERS.append(DecisionWriter(DATABASE))

    def purge_expired() -> None:
        # Expired input records every ten minutes; decision rows past their retention once a day
        # (open question Q45). Every replica does this; a purge that finds nothing does nothing.
        wait, decisions_purged = threading.Event(), None
        while not wait.wait(600):
            try:
                SOURCES.purge()
            except Exception as exc:    # noqa: BLE001 -- tried again in ten minutes
                log.error("expired input records could not be purged: %s", type(exc).__name__)
            if decisions_purged is None or time.monotonic() - decisions_purged >= 86_400:
                try:
                    decisions_db.purge(DATABASE)
                    decisions_purged = time.monotonic()
                except Exception as exc:    # noqa: BLE001 -- tried again in ten minutes
                    log.error("old decision rows could not be purged: %s", type(exc).__name__)

    threading.Thread(target=purge_expired, name="guardrail-purge", daemon=True).start()


if db.configured():
    _use_database()
TRACER = tracing.start()            # None unless an OTLP endpoint is configured
if TRACER is not None:
    engine.OBSERVERS.append(TRACER)
classifier.warm_up(RULES.get())  # fails at startup if the policy is missing or invalid; loads the model if it is on

app = FastAPI(title="IndoLog guardrail", version="0.5.0", telemetry=tracing.FASTAPI_TELEMETRY, description=(
    "Sits before and after the model; it never calls one itself. "
    "**Input** (before): screens the request and documents, refuses clear out-of-scope requests, and on a "
    "pass returns the system prompt and user message to send to the model. "
    "**Answer** (after): checks the model's reply and always returns something to show the user. "
    "**Output** (after, for extractions): validates the extracted fields. Every rule is in rules.json."))


if TRACER is not None:
    app.add_middleware(tracing.TraceParent)


def _examples(examples: dict) -> dict:
    return {name: {"summary": summary, "value": value} for name, (summary, value) in examples.items()}


@app.post("/guardrail/input", response_model=GuardrailResponse)
def guardrail_input(request: InputRequest = Body(openapi_examples=_examples(INPUT_EXAMPLES))):
    """Before the model. On pass or review, send output.system_instructions as the system prompt and
    output.user_message as the user message. Otherwise don't call the model; show `message`."""
    return run_input(request, RULES.get(), SOURCES, defer=AFTER.submit)


@app.post("/guardrail/answer", response_model=AnswerResponse)
def guardrail_answer(request: AnswerRequest = Body(openapi_examples=_examples(ANSWER_EXAMPLES))):
    """After the model. Send exactly what it replied; show the `answer` that comes back."""
    return run_answer(request, RULES.get(), SOURCES)


@app.post("/guardrail/output", response_model=GuardrailResponse)
def guardrail_output(request: OutputRequest = Body(openapi_examples=_examples(OUTPUT_EXAMPLES))):
    """After the model, for extractions: validates the extracted fields before they're passed on."""
    return run_output(request, RULES.get(), SOURCES)


@app.post("/guardrail", response_model=GuardrailResponse, include_in_schema=False)
def guardrail_legacy(request: OutputRequest):
    """The first version's address for the output check."""
    return run_output(request, RULES.get(), SOURCES)


@app.get("/rules")
def rules():
    """The rules in force right now."""
    current = RULES.get()
    return {"file": RULES.describe(), "error": RULES.error, **current.model_dump()}


@app.get("/health")
def health():
    current = RULES.get()
    return {"status": "ok", "rules_version": current.version, "rules_error": RULES.error,
            "models": classifier.status(current), "policy_source": RULES.describe(),
            # Shadow checks past their review_by date. Listing them is all that happens: the check
            # stays in shadow, and `status` stays "ok", until someone changes the policy.
            "review_overdue": current.past_review(datetime.now(timezone.utc).date())}


if DATABASE is not None:
    # A separate application with its own OpenAPI document and its own authentication: the check
    # endpoints' contract does not change because policy administration exists.
    from . import admin
    admin.state.update(engine=DATABASE, store=RULES)
    app.mount("/guardrail/policy", admin.app)
