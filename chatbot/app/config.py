"""Everything the app is told comes from environment variables, read here and nowhere else.

start.sh puts them there from one private file in the home folder (README.md). No settings file
inside this folder is read: a key written here would be one careless commit away from being
published.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields

# LangChain sends every prompt and reply to LangSmith when its tracing variables are set. The
# prompts here hold test emails, so tracing is off whatever the environment says. This runs before
# any LangChain module is imported (app/backend/llm/provider.py imports them only when called).
TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2")
for _name in TRACING_VARIABLES:
    os.environ[_name] = "false"

LOOPBACK = ("127.0.0.1", "localhost", "::1")
LANGFUSE_CLOUD = "https://cloud.langfuse.com"      # Langfuse Cloud, EU region


def _text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _number(name: str, default: float) -> float:
    try:
        return float(_text(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # The guardrail. `guardrail_url` and `guardrail_timeout_ms` are the two names the copied
    # gateway (app/guardrail/gateway.py) reads.
    guardrail_url: str
    guardrail_timeout_ms: int
    # Where every run is written, one row each. start.sh makes it `logs` in this tool's folder.
    log_dir: str
    # A shared password. Needed whenever the app can be reached from another machine.
    password: str
    # "none": guardrail-only. A provider's name: that provider and no other. Empty: every
    # provider configured below, the next one tried whenever a call fails.
    model: str
    # Off: the model is called only after a pass. On: also after a review the backend answers
    # with a caution, as the backend itself does.
    call_model_on_review: bool
    model_timeout_seconds: float
    # The chatbot's model providers. One that is not set here is never called.
    ollama_base_url: str
    openai_api_key: str
    groq_api_key: str
    google_api_key: str
    # A model name to use in place of a provider's default. Empty: the default.
    # Providers retire models; this is how a tester moves on without a code change.
    ollama_model: str
    openai_model: str
    groq_model: str
    gemini_model: str
    # The testers' Langfuse project (app/langfuse_trace.py). Off unless both keys are set. Every run
    # is sent there in full: the email, the question, the prompts and the replies.
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str
    # Who is testing, as Langfuse shows it on each of their runs. Empty: nobody named.
    tester: str


def load() -> Settings:
    return Settings(
        guardrail_url=_text("GUARDRAIL_URL", "http://localhost:8001"),
        guardrail_timeout_ms=int(_number("GUARDRAIL_TIMEOUT_MS", 10_000)),
        log_dir=_text("CHATBOT_LOG_DIR"),
        password=os.environ.get("CHATBOT_PASSWORD", ""),
        model=_text("CHATBOT_MODEL").lower(),
        call_model_on_review=_text("CHATBOT_CALL_MODEL_ON_REVIEW") == "1",
        model_timeout_seconds=_number("CHATBOT_MODEL_TIMEOUT_SECONDS", 120.0),
        ollama_base_url=_text("OLLAMA_BASE_URL"),
        openai_api_key=_text("OPENAI_API_KEY"),
        groq_api_key=_text("GROQ_API_KEY"),
        google_api_key=_text("GOOGLE_API_KEY"),
        ollama_model=_text("CHATBOT_OLLAMA_MODEL"),
        openai_model=_text("CHATBOT_OPENAI_MODEL"),
        groq_model=_text("CHATBOT_GROQ_MODEL"),
        gemini_model=_text("CHATBOT_GEMINI_MODEL"),
        langfuse_public_key=_text("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_text("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=(_text("LANGFUSE_BASE_URL") or LANGFUSE_CLOUD).rstrip("/"),
        tester=_text("CHATBOT_TESTER"),
    )


# One object, read by the copied gateway at call time. `reload()` re-reads the environment into it.
settings = load()


def reload() -> Settings:
    fresh = load()
    for field in fields(Settings):
        setattr(settings, field.name, getattr(fresh, field.name))
    return settings
