"""What a model call comes to, and how the flow asks a model for a reply.

flow.py asks for a reply through `Model` and reads the three fields of `ModelReply`. The model
this app has is in app/backend/llm/provider.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelReply:
    """What a model call came to. Exactly one of `text`, `refused` and `error` says what happened."""

    text: str | None = None        # what the model wrote
    refused: str | None = None     # the provider or the model declined: "content_filter_prompt",
    #                                "content_filter_reply" or "model_refusal". A result, not an error.
    error: str | None = None       # the call failed: "timeout", "unreachable", "http_429",
    #                                "model_not_found", "bad_response"


class Model(Protocol):
    name: str

    def complete(self, system: str, user: str, *, json_schema: dict | None = None) -> ModelReply:
        """One call: `system` as the system prompt, `user` as the user message, both exactly as the
        guardrail returned them."""
