"""OpenTelemetry spans for every decision: one for the stage, one under it for each check.

Open question Q39.6, decided by the owner on 3 Oct 2026: whether Langfuse runs in the deployed
environment could not be told from the repository, so the guardrail speaks OpenTelemetry, which
Langfuse and Azure Monitor both ingest. Where the spans go is the standard environment:

    OTEL_EXPORTER_OTLP_ENDPOINT   e.g. http://otel-collector:4318, or Langfuse's
                                  https://<host>/api/public/otel with its Basic auth in
    OTEL_EXPORTER_OTLP_HEADERS    Authorization=Basic <base64 of public:secret>
    OTEL_SERVICE_NAME             indolog-guardrail by default

With no endpoint set, nothing is traced and none of the OpenTelemetry packages is imported (they
are the `otel` extra).

A span carries what the decision log line carries: the request id, the stage, what the caller was
told and what it would have been told, the policy version, and per check its mode, verdict, reason,
score, time and measures. Numbers and ids. NEVER text: not the email, not a question, not an
extracted value, not a matched snippet.

Spans are written after the fact, from the finished decision, with each check's real start and end
times. The stage span ends when the decision is recorded; where the policy runs a shadow check after
the response, that is after the caller had its answer, and the check's own span shows when it ran.
If the caller sends a W3C `traceparent` header the stage span is its child, so a guardrail check
appears inside the backend's trace of the same request.
"""
from __future__ import annotations

import contextvars
import logging
import os
import time

log = logging.getLogger("guardrail")

# FastAPI has telemetry of its own (new in 0.142; read in 0.142.2): with the OpenTelemetry SDK
# installed and an OTLP endpoint in the environment it exports request spans, metrics and LOGS by
# itself. Its log signal records unhandled exceptions "including exception messages and stack
# traces", and an exception message can quote the text it failed on; it also keeps the body that
# failed validation where any log processor can read it. This service's rule is narrower: the type
# of an exception and nothing else. So FastAPI's telemetry is switched off, on every FastAPI app the
# guardrail makes, and the only spans that leave the process are the ones built below.
FASTAPI_TELEMETRY = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                     "auto_configure": False}

# The caller's trace context for the request being handled, set by the service from its headers.
parent: contextvars.ContextVar = contextvars.ContextVar("guardrail_trace_parent", default=None)


def endpoint() -> str | None:
    return os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None


class SpanObserver:
    """An engine observer: `engine.OBSERVERS.append(SpanObserver(tracer))`."""

    def __init__(self, tracer) -> None:
        self.tracer = tracer

    def __call__(self, decision) -> None:
        from opentelemetry import trace
        from opentelemetry.context import Context
        from opentelemetry.trace import Status, StatusCode

        ended = time.time_ns()
        started = min([o.started_ns for o in decision.outcomes if o.started_ns] + [ended])
        # Under the caller's trace if it sent one, and otherwise a trace of its own: never under
        # whatever span happens to be current on this thread.
        stage = self.tracer.start_span(f"guardrail.{decision.stage}", context=parent.get() or Context(), start_time=started, attributes={
            "guardrail.request_id": decision.request_id, "guardrail.stage": decision.stage,
            "guardrail.status": decision.status, "guardrail.would_be": decision.would_be,
            "guardrail.policy_version": decision.rules_version,
            **{f"guardrail.measure.{name}": value for name, value in decision.measures.items()}})
        inside = trace.set_span_in_context(stage)
        for outcome in decision.outcomes:
            began = outcome.started_ns or started
            attributes = {"guardrail.check.id": outcome.filter_id, "guardrail.check.mode": outcome.mode,
                          "guardrail.check.verdict": outcome.verdict, "guardrail.check.error": outcome.error,
                          **{f"guardrail.measure.{name}": value for name, value in outcome.measures.items()}}
            if outcome.reason_code:
                attributes["guardrail.check.reason"] = outcome.reason_code
            if outcome.rules:
                attributes["guardrail.check.rules"] = outcome.rules
            if outcome.score is not None:
                attributes["guardrail.check.score"] = float(outcome.score)
            span = self.tracer.start_span(f"guardrail.check.{outcome.filter_id}", context=inside, start_time=began,
                                          attributes=attributes)
            if outcome.error:
                span.set_status(Status(StatusCode.ERROR, outcome.reason_code or "error"))
            span.end(end_time=began + int(outcome.latency_ms * 1e6))
        stage.end(end_time=ended)


class TraceParent:
    """ASGI middleware: keeps the caller's trace context (the W3C `traceparent` header) for the
    spans of this request. It reads two headers and nothing else."""

    _HEADERS = (b"traceparent", b"tracestate")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            sent = {name.decode("latin-1"): value.decode("latin-1")
                    for name, value in scope.get("headers", ()) if name in self._HEADERS}
            if sent:
                from opentelemetry.propagate import extract

                parent.set(extract(sent))
        await self.app(scope, receive, send)


def start():
    """The observer to register, exporting over OTLP/HTTP in batches; or None when no endpoint is
    configured or the packages are not installed. Never raises: tracing is an observer."""
    if endpoint() is None:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", "indolog-guardrail")}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        log.info("tracing: OpenTelemetry spans are exported over OTLP")
        return SpanObserver(provider.get_tracer("guardrail"))
    except Exception as exc:        # noqa: BLE001
        log.error("tracing could not be started (%s); continuing without it", type(exc).__name__)
        return None
