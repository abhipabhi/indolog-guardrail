FROM python:3.12-slim

WORKDIR /app

# Dependencies first, from pyproject.toml alone and a placeholder package, so that a change to the
# code does not reinstall torch or fetch the model again.
#
# The bundled policy runs the injection classifier on the question part, in shadow, so the image
# carries its runtime and its model. The CPU-only build of torch: the default Linux wheel pulls the
# CUDA libraries, gigabytes this service never uses. The `postgres` extra is the database driver,
# the migrations' tooling and token validation for the admin API. The `otel` extra is the trace
# exporter; it does nothing until OTEL_EXPORTER_OTLP_ENDPOINT is set (guardrail/tracing.py).
COPY pyproject.toml ./
RUN mkdir guardrail && touch guardrail/__init__.py \
 && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu "torch==2.14.1+cpu" \
 && pip install --no-cache-dir ".[classifier,postgres,otel]"

# The model, at its pinned revision, fetched while the image is built and never while it serves.
# protectai/deberta-v3-base-prompt-injection-v2 is Apache-2.0. No Llama weights in any image until
# legal clears them (libs/guardrail/CLAUDE.md).
COPY scripts/fetch_model.py ./scripts/fetch_model.py
RUN python scripts/fetch_model.py protectai/deberta-v3-base-prompt-injection-v2 \
        90c9989b1a342275dd0d1a95aad283c04e075671 --out /app/models
ENV GUARDRAIL_MODELS_DIR=/app/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# The code itself (with its migrations), over the placeholder.
COPY guardrail ./guardrail
RUN pip install --no-cache-dir --no-deps --force-reinstall .

# Both policies are in the image. It starts with the ROLLOUT policy. Six checks are enforced in
# it: the four every policy enforces (the injection rules, hidden characters, encoded content,
# `ingest`) and, by the owner's choice, the output `schema` check and the reply's `leakage` check.
# Every other check is in shadow:
# nothing it finds (or its failing to run) changes an answer; every decision is written down with
# its scores and the rules that fired, and a check that could not run is in it as `not_observed`.
# Moving a shadow check to enforce is a policy change (see scripts/rollout_policy.py), not a new
# image.
COPY rules.json rules.rollout.json ./
ENV GUARDRAIL_RULES=/app/rules.rollout.json

RUN useradd --uid 10001 --no-create-home appuser
USER 10001

EXPOSE 8000
CMD ["uvicorn", "guardrail.app:app", "--host", "0.0.0.0", "--port", "8000"]
