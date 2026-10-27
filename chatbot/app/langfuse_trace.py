"""Every run in Langfuse: the message, each call to the guardrail, the model call, and what came of it.

Off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are in the settings file. They are the keys
of the testers' own Langfuse project, never the platform's: start.sh and start.ps1 take them from
~/.guardrail-chatbot.env alone, and the client below is given them and nothing from the environment.

FULL TEXT (the owner's decision, 27 Oct 2026). A trace holds the email, the attachment, the
question, every request to the guardrail and its whole answer, the prompt the model was sent and
its reply. So it holds whatever a tester pasted: fake or anonymised emails only, as everywhere in
this app. The screen says so before anything is sent.

One trace per run:

    chatbot chat                  the message in; what the user saw, the status and the rules out
      guardrail input             the question alone: the request and the guardrail's answer
        guardrail.input           the guardrail's own spans, one per check, when the guardrail was
          guardrail.check.*       started by start.sh with the keys (they are codes, never text)
      guardrail input             the question with the email
      model                       the system prompt and the user message sent; the reply
      guardrail answer            the reply: the request and the guardrail's answer

An email run is `chatbot email rfq`, with `guardrail output` for the extraction. The trace is scored
"guardrail" with the run's status (pass, review, fail, needs review), tagged with the kind of run,
its user is CHATBOT_TESTER when that is set, and its session is the browser tab.

The platform's backend/app/services/guardrail_langfuse.py was the model for this: a trace per piece
of work, an observation of type "guardrail" per check, a categorical score. It is not imported or
copied. Like it, this is an observer only: every call here swallows its own errors (logged once),
Langfuse being unreachable or refusing the keys never changes a run, and nothing waits on the
network, since the SDK sends in the background.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
from typing import Any, Callable

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# How each status shows in Langfuse. A guardrail that could not be used is an error; a stop is too,
# as the platform shows it, so that stopped runs stand out in the list.
_LEVELS = {"pass": "DEFAULT", "answered": "DEFAULT", "review": "WARNING", "no_answer": "WARNING",
           "out_of_scope": "WARNING", "fail": "ERROR", "needs review": "ERROR"}

_client = None
_lock = threading.Lock()
_warned = False
_project: dict[str, str | None] = {"id": None, "problem": None}     # filled in the background, once
# Set while a run is traced: only then is a call to the guardrail an observation.
_tracing: contextvars.ContextVar[bool] = contextvars.ContextVar("chatbot_langfuse_run", default=False)


def enabled() -> bool:
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def client():
    """The process's Langfuse client, made once; None when Langfuse is off or could not be set up."""
    global _client
    if not enabled():
        return None
    with _lock:
        if _client is None:
            try:
                from langfuse import Langfuse

                _client = Langfuse(public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key,
                                   base_url=settings.langfuse_base_url, sample_rate=1.0, flush_interval=1.0)
            except Exception:  # noqa: BLE001  an observer never stops the work
                _warn("could not be set up")
                return None
            threading.Thread(target=_look_up_project, args=(_client,), daemon=True).start()
    return _client


def _look_up_project(langfuse) -> None:
    """The project the keys belong to, for each trace's address; or why there is none. In the
    background: a Langfuse that does not answer must not hold up the screen."""
    try:
        found = langfuse.api.projects.get()
        _project["id"] = found.data[0].id if found.data else None
        if _project["id"] is None:
            _project["problem"] = "Langfuse answered, but these keys belong to no project."
    except Exception as problem:  # noqa: BLE001
        code = getattr(problem, "status_code", None)
        _project["problem"] = (f"Langfuse did not accept the keys in your settings file (HTTP {code}). Check them, and "
                               "LANGFUSE_BASE_URL, which must be the region the project is in." if code in (401, 403) else
                               f"Langfuse could not be reached at {settings.langfuse_base_url} "
                               f"({f'HTTP {code}' if code else type(problem).__name__}). Runs are not traced until it can be.")


def problem() -> str | None:
    """Why runs are not reaching Langfuse, when that is known."""
    return _project["problem"] if enabled() else None


def _warn(what: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning("Langfuse tracing %s; carrying on without it.", what, exc_info=True)


def _safely(call: Callable[[], Any]) -> None:
    try:
        call()
    except Exception:  # noqa: BLE001
        _warn("failed")


def _ascii(value: str | None) -> str | None:
    """Langfuse takes US-ASCII ids of at most 200 characters for a user and a session."""
    text = (value or "").strip()
    return text[:200] if text and text.isascii() else None


# --- a run ------------------------------------------------------------------------------------------

def traced_run(message, run: Callable[[], Any], *, session: str | None = None, edited_prompt: bool = False):
    """`run()` (chat.run_message for `message`) as one trace. Returns the run, and the trace as
    {"id", "url"}: None when Langfuse is off; its url None until the project is known."""
    from app import chat

    langfuse = client()
    if langfuse is None:
        return run(), None
    email = chat.mode(message) == chat.EMAIL
    name = f"chatbot email {message.intent}" if email else "chatbot chat"
    stack = contextlib.ExitStack()
    try:
        from langfuse import propagate_attributes

        root = stack.enter_context(langfuse.start_as_current_observation(as_type="span", name=name, input={
            "email": message.email, "attachment": message.attachment, "question": message.question,
            "intent": message.intent or chat.CHAT}))
        stack.enter_context(propagate_attributes(
            trace_name=name, session_id=_ascii(session), user_id=_ascii(settings.tester),
            tags=["guardrail-chatbot", chat.EMAIL if email else chat.CHAT] + (["edited system prompt"] if edited_prompt else [])))
    except Exception:  # noqa: BLE001
        _safely(stack.close)
        _warn("could not start a trace")
        return run(), None
    token = _tracing.set(True)
    try:
        done = run()
        status = chat.status(done)
        _safely(lambda: root.update(
            output={"user_saw": chat.user_would_see(done), "guardrail": status, "rules": chat.rule_ids(done),
                    "outcome": done.outcome.title},
            level=_LEVELS.get(status, "WARNING"),
            metadata={"policy": done.policy, "model": done.model_mode, "model_called": done.model_called,
                      "request_ids": done.request_ids, "outcome": done.outcome.kind, "edited_system_prompt": edited_prompt}))
        _safely(lambda: langfuse.score_current_trace(name="guardrail", value=status, data_type="CATEGORICAL"))
        trace_id = getattr(root, "trace_id", None)
    except BaseException as problem:
        _safely(lambda: root.update(level="ERROR", status_message=type(problem).__name__))
        raise
    finally:
        _tracing.reset(token)
        _safely(stack.close)
    if not trace_id:
        return done, None
    url = f"{settings.langfuse_base_url}/project/{_project['id']}/traces/{trace_id}" if _project["id"] else None
    return done, {"id": trace_id, "url": url}


# --- the model --------------------------------------------------------------------------------------

class _Generation:
    """Stands between the flow and the model: each call is a "generation" in the run's trace,
    with exactly what was sent and what came back. Everything else is the model's own."""

    def __init__(self, model) -> None:
        self._model = model
        self.name = model.name

    def __getattr__(self, name: str):
        return getattr(self._model, name)          # answered_by, tries

    def complete(self, system: str, user: str, *, json_schema: dict | None = None):
        langfuse = client() if _tracing.get() else None
        observing, observation = None, None
        if langfuse is not None:
            try:
                observing = langfuse.start_as_current_observation(
                    as_type="generation", name="model", model=self._model.name,
                    input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    metadata={"provider": self._model.name, "fields_as_json": json_schema is not None})
                observation = observing.__enter__()
            except Exception:  # noqa: BLE001
                observing, observation = None, None
                _warn("could not record a model call")
        try:
            reply = self._model.complete(system, user, json_schema=json_schema)
        except BaseException as problem:
            if observing is not None:
                _safely(lambda: observation.update(level="ERROR", status_message=type(problem).__name__))
                _safely(lambda: observing.__exit__(None, None, None))
            raise
        if observing is not None:
            _safely(lambda: observation.update(**_generation(reply, self._model)))
            _safely(lambda: observing.__exit__(None, None, None))
        return reply


def _generation(reply, model) -> dict:
    update: dict[str, Any] = {"metadata": {"provider": model.name, "tries": getattr(model, "tries", None)}}
    if getattr(model, "answered_by", None):
        update["model"] = model.answered_by
    usage = getattr(model, "usage", None)
    if usage:
        update["usage_details"] = usage
    if reply.text is not None:
        update["output"] = reply.text
    elif reply.refused:
        update.update(output={"refused": reply.refused}, level="WARNING", status_message=f"refused: {reply.refused}")
    else:
        update.update(output={"error": reply.error}, level="ERROR", status_message=f"error: {reply.error}")
    return update


def traced_model(model):
    """The model, its calls recorded in the run's trace; the model itself when Langfuse is off."""
    return _Generation(model) if model is not None and enabled() else model


# --- the guardrail ----------------------------------------------------------------------------------

class _GuardrailCall(httpx.AsyncBaseTransport):
    """Each check request a traced run sends the guardrail becomes a "guardrail" observation, with the
    request and the whole answer. The request carries the observation's W3C `traceparent`, so a
    guardrail that exports to the same project (start.sh starts it so) puts its own spans inside it."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        langfuse = client() if _tracing.get() else None
        if langfuse is None or not request.url.path.startswith("/guardrail/"):
            return await self._inner.handle_async_request(request)
        try:
            sent = json.loads(request.content or b"{}")
        except ValueError:
            sent = None
        stage = request.url.path.rsplit("/", 1)[-1]
        try:
            observing = langfuse.start_as_current_observation(
                as_type="guardrail", name=f"guardrail {stage}", input=sent,
                metadata={"request_id": sent.get("request_id") if isinstance(sent, dict) else None})
            observation = observing.__enter__()
        except Exception:  # noqa: BLE001
            _warn("could not record a guardrail call")
            return await self._inner.handle_async_request(request)
        _safely(lambda: _carry_trace(request))
        try:
            response = await self._inner.handle_async_request(request)
            content = await response.aread()
            await response.aclose()
        except BaseException as problem:
            _safely(lambda: observation.update(output={"status": "unreachable"}, level="ERROR",
                                               status_message=f"the guardrail could not be reached ({type(problem).__name__})"))
            _safely(lambda: observing.__exit__(None, None, None))
            raise
        _safely(lambda: observation.update(**_guardrail(response.status_code, content)))
        _safely(lambda: observing.__exit__(None, None, None))
        return httpx.Response(response.status_code, content=content, request=request,
                              headers={"content-type": response.headers.get("content-type", "application/json")})

    async def aclose(self) -> None:
        await self._inner.aclose()


def _carry_trace(request: httpx.Request) -> None:
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier)
    for name in ("traceparent", "tracestate"):
        if carrier.get(name):
            request.headers[name] = carrier[name]


def _guardrail(http_status: int, content: bytes) -> dict:
    try:
        body = json.loads(content)
    except ValueError:
        body = None
    if http_status != 200 or not isinstance(body, dict):
        return {"output": body if body is not None else {"http_status": http_status}, "level": "ERROR",
                "status_message": f"HTTP {http_status}", "metadata": {"http_status": http_status}}
    status = str(body.get("status"))
    rules = [str(f.get("rule") or f.get("check")) for f in body.get("failures") or [] if isinstance(f, dict)]
    return {"output": body, "level": _LEVELS.get(status, "WARNING"),
            "metadata": {"status": status, "rules_version": body.get("rules_version"), "rules": rules}}


def install() -> None:
    """Puts the guardrail calls in the trace, once a process, when Langfuse is on. The connection is
    the copied guardrail_io's own hook (`connect`), so no copied file is changed."""
    from app.guardrail import guardrail_io

    if client() is None or getattr(guardrail_io.connect, "langfuse", False):
        return                  # (made here, so that a project the keys do not open is on the screen from the start)
    inner = guardrail_io.connect

    def connect() -> httpx.AsyncBaseTransport:
        return _GuardrailCall(inner())

    connect.langfuse = True
    guardrail_io.connect = connect
