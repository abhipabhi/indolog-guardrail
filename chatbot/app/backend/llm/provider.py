"""The model providers this app can call.

Testers use Google Gemini with their own key. Three more providers are wired the same way
(OpenAI, Groq and Ollama); none of those three has ever been run against the real service.

  * A provider is used only when it is configured: its key, or for Ollama its address. With none
    configured there is no model at all, and the app is guardrail-only.
  * CHATBOT_MODEL chooses: `none` is guardrail-only, a provider's name (gemini, openai, groq,
    ollama) is that provider and no other, and left empty it is every configured provider, the
    next one tried whenever a call fails. A name that is not one of these, or a provider whose key
    is missing, calls no model at all and says so on the screen: a typing mistake never widens
    where a test email goes.
  * No tools are bound. Each call carries exactly two messages: the system prompt and the user
    message the guardrail returned.
  * For an incoming email the reply must be JSON with the fields of the chosen intent. That is
    asked for through each provider's own structured-output setting, which is not a tool.
  * Temperature 0 is asked for, so that the same test gives the same reply as far as a provider
    allows. Some newer Gemini models ignore it.
  * Each provider has a default model, which CHATBOT_GEMINI_MODEL and its siblings replace.
    Providers retire models; a call for one that is gone comes back as "model not found".
  * A call comes back as text, a refusal code or an error code. The text of a provider's error is
    never kept or shown: it can quote the prompt, which holds the test email.

Test emails go to every provider configured here. `describe()` says which, and the screen shows
it before anything is sent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from app.config import Settings
from app.guardrail.model import ModelReply


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    model: str              # the default, unless the tester names another
    variable: str           # the environment variable that switches it on
    setting: str            # the field of Settings that holds its value
    model_variable: str     # the environment variable that names another model

    @property
    def note(self) -> str:
        if self.id == "ollama" and self.model.endswith("-cloud"):
            return "a model with Ollama's -cloud tag is run by Ollama's hosted service, not on this machine"
        return ""


# In the order they are tried when CHATBOT_MODEL is left empty.
PROVIDERS = (
    Provider("ollama", "Ollama", "gpt-oss:120b-cloud", "OLLAMA_BASE_URL", "ollama_base_url", "CHATBOT_OLLAMA_MODEL"),
    Provider("openai", "OpenAI", "gpt-4o-mini", "OPENAI_API_KEY", "openai_api_key", "CHATBOT_OPENAI_MODEL"),
    Provider("groq", "Groq", "qwen/qwen3-32b", "GROQ_API_KEY", "groq_api_key", "CHATBOT_GROQ_MODEL"),
    Provider("gemini", "Google Gemini", "gemini-3.5-flash", "GOOGLE_API_KEY", "google_api_key", "CHATBOT_GEMINI_MODEL"),
)

GUARDRAIL_ONLY = "none"
# A finish reason that means the provider cut the reply for its content, by provider.
_FILTERED = {"content_filter", "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def problem(settings: Settings) -> str | None:
    """Why CHATBOT_MODEL cannot be done as written, for the screen. None when it can.
    The value itself is never repeated: whatever was typed there is not shown."""
    chosen = {provider.id: provider for provider in PROVIDERS}.get(settings.model)
    if settings.model in ("", GUARDRAIL_ONLY):
        return None
    if chosen is None:
        return (f"CHATBOT_MODEL is not one of: {GUARDRAIL_ONLY}, {', '.join(p.id for p in PROVIDERS)}. "
                "No model is called until it is.")
    if not getattr(settings, chosen.setting):
        return f"CHATBOT_MODEL is {chosen.id}, but {chosen.variable} is not set. No model is called until it is."
    return None


def configured(settings: Settings) -> list[Provider]:
    """The providers that will be called, in order. Empty: guardrail-only."""
    if settings.model == GUARDRAIL_ONLY or problem(settings):
        return []
    return [replace(provider, model=getattr(settings, f"{provider.id}_model") or provider.model)
            for provider in PROVIDERS if getattr(settings, provider.setting) and settings.model in ("", provider.id)]


def describe(settings: Settings) -> list[str]:
    """Where a test email goes, one line a provider, for the screen."""
    return [f"{p.name} ({p.model})" + (f": {p.note}" if p.note else "") for p in configured(settings)]


def _client(provider: Provider, settings: Settings) -> Any:
    """One LangChain chat model. Imported here, so guardrail-only mode loads no provider code."""
    seconds = settings.model_timeout_seconds
    if provider.id == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=provider.model, base_url=settings.ollama_base_url, temperature=0,
                          client_kwargs={"timeout": seconds})
    if provider.id == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=provider.model, api_key=settings.openai_api_key, temperature=0,
                          timeout=seconds, max_retries=1)
    if provider.id == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=provider.model, api_key=settings.groq_api_key, temperature=0,
                        timeout=seconds, max_retries=1)
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=provider.model, api_key=settings.google_api_key, temperature=0,
                                  timeout=seconds, max_retries=1)


MODEL_NOT_FOUND = "model_not_found"


def _status(problem: BaseException) -> int | None:
    for name in ("status_code", "code"):
        if isinstance(getattr(problem, name, None), int):
            return getattr(problem, name)
    status = getattr(getattr(problem, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _error_code(problem: Exception) -> str:
    """A provider failure as a code. Never its message."""
    # LangChain wraps a provider's own error; the HTTP status is often on the one underneath.
    chain = [problem, problem.__cause__, problem.__context__]
    kinds = " ".join(type(link).__name__ for link in chain if link is not None).lower()
    status = next((found for link in chain if link is not None and (found := _status(link)) is not None), None)
    if "modelnotfound" in kinds or (status == 404 and "notfound" in kinds):
        return MODEL_NOT_FOUND          # the provider does not offer this model, or not to this key
    if status is not None:
        return f"http_{status}"
    if "timeout" in kinds:
        return "timeout"
    if "connect" in kinds:
        return "unreachable"
    return f"error_{type(problem).__name__}"


def _text(content: Any) -> str:
    """A reply's text. Some providers return a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part if isinstance(part, str) else str(part.get("text", ""))
                       for part in content if isinstance(part, (str, dict)))
    return ""


def _shaped(client: Any, schema: dict) -> Any:
    """The same client, told that its reply must be JSON of this shape. What it then returns holds
    the model's own message under `raw`. The name is one some providers require a schema to have."""
    return client.with_structured_output({"title": "extraction", **schema}, method="json_schema", include_raw=True)


class Chain:
    """The configured providers, the next one tried whenever a call fails. This is what the flow
    (app/guardrail/flow.py) asks for a reply."""

    def __init__(self, providers: list[Provider], settings: Settings, clients: list[Any] | None = None) -> None:
        self.providers = providers
        self.name = " > ".join(provider.id for provider in providers)
        self._clients = clients if clients is not None else [_client(provider, settings) for provider in providers]
        self.answered_by: str | None = None     # the model that wrote the last reply, when it said

    def complete(self, system: str, user: str, *, json_schema: dict | None = None) -> ModelReply:
        from langchain_core.messages import HumanMessage, SystemMessage

        self.answered_by = None
        try:
            first, *rest = self._clients if json_schema is None else [_shaped(client, json_schema) for client in self._clients]
            llm = first.with_fallbacks(rest) if rest else first
            message = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        except Exception as problem:  # noqa: BLE001  every provider raises its own types
            if getattr(problem, "code", None) == "content_filter":
                return ModelReply(refused="content_filter_prompt")
            return ModelReply(error=_error_code(problem))
        parsed = None
        if json_schema is not None and isinstance(message, dict):
            message, parsed = message.get("raw"), message.get("parsed")
        details = getattr(message, "response_metadata", None) or {}
        self.answered_by = details.get("model_name") or details.get("model") or None
        # The model's own words. Only where a provider returns the fields and no text are they written out again.
        text = _text(getattr(message, "content", "")) or (json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else "")
        if details.get("finish_reason") in _FILTERED:
            return ModelReply(refused="content_filter_reply")
        if (details.get("prompt_feedback") or {}).get("block_reason"):
            return ModelReply(refused="content_filter_prompt")
        if not text.strip():
            return ModelReply(error="bad_response")
        return ModelReply(text=text)


def model_for(settings: Settings) -> Chain | None:
    """The model for this run, or None for guardrail-only."""
    providers = configured(settings)
    return Chain(providers, settings) if providers else None
