# guardrail

IndoLog's guardrail. It sits before and after the model. It never calls a model itself, so it
needs no model key, and there is no model inside it to talk round.

```
email text + question ─> POST /guardrail/input ─> model ─> POST /guardrail/answer ─> shown to the user
                             (before)             (yours)      (after, for a reply)
                                                           └─> POST /guardrail/output
                                                               (after, for an extraction)
```

The service that calls the model calls the guardrail on both sides:

1. Send the question and the documents (email body, attachment text) to `/guardrail/input`.
   - `pass` or `review`: call the model. `review` means a person should look at the result.
   - `fail` or `out_of_scope`: do not call the model; show `message`.
2. Send what the model returned to `/guardrail/answer` (a reply) or `/guardrail/output` (extracted
   fields), under the same `request_id`. Show or store only what comes back.

**To test it, use the chatbot** in [`../chatbot`](../chatbot/README.md): it makes these calls for
you and shows what the guardrail did at each step.

## What is in this folder

Only what the service is built from and runs on.

| File or folder | What it is |
|---|---|
| `guardrail/` | The service's code, with its database migrations |
| `rules.json` | The bundled policy: every check, its mode and its rules |
| `rules.rollout.json` | The rollout policy, which the image starts with (see below) |
| `Dockerfile`, `docker-compose.yml` | The image, and the guardrail with its database for a local run |
| `scripts/fetch_model.py` | Downloads the classifier's model at a pinned revision; the image build runs it |
| `pyproject.toml` | The package and its pinned dependencies |

The guardrail's tests, its evaluation data and reports, and its design documents (the threat
model, the description of every check, the runbook) are not in this repository. Comments in the
code that point to `tests/`, `docs/guardrail/`, `CLAUDE.md` or to scripts other than
`fetch_model.py` point to those.

## Run it

In containers, as it deploys (the guardrail limited to 2 CPUs, Postgres 16 beside it, the rollout
policy). You need Docker Desktop, running, and about 10 GB of free disk space: the image is about
3 GB and building it needs room.

```bash
docker compose up --build -d
```

The first time, this builds the image, which takes several minutes and needs an internet
connection. The classifier's model is downloaded as part of that build.

```bash
docker compose ps
```

The `guardrail` line should say `(healthy)`. Then:

```bash
curl http://localhost:8001/health
```

should show `"status":"ok"`, `"rules_error":null`, a `"rules_version"` that ends in `-rollout`
and `"injection_classifier":"loaded"`. http://localhost:8001/docs has examples for each endpoint.
The service listens on this machine only (`127.0.0.1:8001`).

To stop it:

```bash
docker compose down
```

`docker compose down -v` also deletes what it has stored: its policy versions and its record of
decisions. Do that before starting a newer version of this folder, because the guardrail keeps
its rules in its own database and would otherwise go on holding the older version's.

Without Docker (Python 3.11 or later):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn guardrail.app:app
```

http://localhost:8000/docs then has the examples. This runs from `rules.json`, keeps what each
input check saw in memory, and writes decisions to the log only. With the classifier (CPU), the
database and tracing:

```bash
pip install -e ".[classifier,postgres,otel]"
python scripts/fetch_model.py protectai/deberta-v3-base-prompt-injection-v2 90c9989b1a342275dd0d1a95aad283c04e075671
```

## The contract

Frozen: paths, request and response schemas, status codes and error shapes.

| Operation | Returns |
|---|---|
| `POST /guardrail/input` | `status`: `pass`, `review`, `fail` or `out_of_scope`; `checks`; `failures` (check, rule, action, message); on pass or review, `output.system_instructions` and `output.user_message` with the documents fenced between random markers |
| `POST /guardrail/output` | The same statuses; on pass or review, `output` is the extracted fields, with any value a check could not stand behind set to null |
| `POST /guardrail/answer` | `status`: `answered`, `no_answer` or `out_of_scope`; `answer` is always something to show; `notes` say what was done |
| `GET /rules` | The policy in force |
| `GET /health` | Policy version and source, whether the classifier's model is loaded, and any shadow check past its review date (`review_overdue`) |

With a database configured there is also an admin API at `/guardrail/policy`, a separate
application with its own OpenAPI document and its own authentication.

## What it checks

Each check is `enforce`, `shadow` or `off` in the policy. The table gives the bundled policy
(`rules.json`). **Always** means the check is enforced in every policy: a policy that puts it in
shadow or switches it off is refused.

| Stage | Check | Bundled mode | In one line |
|---|---|---|---|
| input | `size_limit` | enforce | A question over its cap is stopped; mail over a cap goes to a person |
| input | `ingest` | enforce, **always** | A document the converter marked as having a page it could not read goes to a person |
| input | `hidden_characters` | enforce, **always** | Invisible characters, and words mixing alphabets |
| input | `injection_rules` | enforce, **always** | Eighteen rules for instructions aimed at the model, matched on the text with disguises undone |
| input | `injection_classifier` | shadow, after the response; review by 4 Nov 2026 | A learned detector, run on the question only |
| input | `encoded_content` | enforce, **always** | A long encoded block |
| input | `scope` | enforce | A clear out-of-scope question (code, a poem) is refused |
| input | `business_risk` | shadow; review by 4 Nov 2026 | Changed bank details, a new consignee, release without originals |
| output | `schema` | enforce | The extraction has the shape the backend's own schema describes |
| output | `allowed_values`, `numbers`, `confidence`, `injection_flag` | enforce | Fixed lists, positive numbers, minimum confidence, the model's own flag |
| output | `leakage` | enforce | A secret, card number or Aadhaar number in a value: removed, review |
| output | `grounding` | off | A value the source text does not state: removed, review |
| output | `high_impact_fields` | enforce | A bank or consignee field with a value always goes to a person |
| answer | `no_code`, `no_leak`, `length` | enforce | No code, no repeated instructions, not too long |
| answer | `leakage` | enforce | Secrets replaced, card and Aadhaar numbers masked to their last four digits |
| answer | `grounding`, `abstention` | off | A date, reference or amount the excerpts do not contain: withheld |

In the **rollout policy** (`rules.rollout.json`, what the image starts with) every check is in
shadow except five: the four marked **always**, and the output `schema` check. It is not
watch-only: from the first request an instruction aimed at the model is stopped, and a document
with an unread page or an extraction of the wrong shape goes to a person. Everything else is
recorded and changes nothing.

The `schema` check is enforced there by choice, not by the code: a policy can put it back in
shadow with a review date. That is the stopgap when the backend changes an extraction schema
before the policy follows; until a matching policy is active, every extraction of that intent
goes to review.

## Injection checks cannot be watch-only

Three rules, checked when a policy is loaded, so they hold for the admin API, the command line and
a policy file alike. A policy that breaks one is refused with the reason (the admin API answers
422) and the policy in force stays in force.

- **Four input checks are enforced in every policy**: `injection_rules`, `hidden_characters`,
  `encoded_content` and `ingest`. They cannot be put in `shadow`, switched `off`, left out of
  `checks`, shadowed through the input stage's `mode`, or pointed away from a part of the request
  they read. There is no override. Every other check, the output `schema` check included, can be
  put in shadow by a policy.
- **Every check in shadow carries a `review_by` date** in the policy (`"review_by": {"scope":
  "2026-12-01"}`, beside `modes`). `GET /health` lists the checks past their date under
  `review_overdue`. Being late changes nothing else: the check stays in shadow until a new
  policy version moves it to enforce, gives it a later date, or switches it off.

- **A policy cannot empty a check.** It must carry every rule of the bundled rule set (18 today)
  in `injection_rules`, and a schema for each of the backend's eight intents. An empty rule list,
  a missing rule or a missing schema is refused, and the refusal names what is missing.

The dates in the two shipped policies (4 Nov 2026) are provisional; set them against the
deployment day.

## Three rules that are in code, not in policy

- **Fail closed.** An enforced check that cannot run (it raises, runs out of time, or its model or
  store is missing) sends the request to a person with a reason code. It never becomes a pass
  and never an HTTP 500.
- **Shadow changes nothing.** A shadow check runs and is recorded. What it finds does not change
  the answer, and neither does its failing to run, which is recorded as `not_observed` with the
  reason. A missing model still shows on `/health`. Which checks may be in shadow at all, and for
  how long without anyone looking, is the section above.
- **No text in logs or traces.** A decision is logged as ids, offsets, hashes, verdicts, scores
  and timings. Never the email, a filename, a question, a reply, an extracted value or the text
  a rule matched.

Every decision is one JSON line on the logger `guardrail.decisions` and, with a database, one row
in `guardrail.decision`. For each check it holds the mode, the verdict, the score, the measures,
the `reason` (the rule that decided the verdict) and `rules`: the id of every rule that fired in
that check, whether the check is enforced or in shadow.

## How well it does, so far

Measured on policy 12, on the input stage. Read every number with its label:

- **302 of 378** synthetic injection attacks stopped. The synthetic set is a **development set**:
  one author wrote its attacks and the rules that catch them, so this shows the rules do what
  they were written to do, and no more.
- **0 of 770** synthetic benign emails flagged.
- **337 of 709** public attack prompts flagged, and **46 of 888** public benign prompts flagged,
  across four public datasets.

No real mail has been measured, and no threshold should be chosen from these numbers. The
rollout policy gives the same four numbers, because the checks that produce them are enforced
there too.

## Configuration

Everything is optional; with nothing set the service runs from the bundled policy.

| Variable | Meaning |
|---|---|
| `GUARDRAIL_RULES` | Path of the policy file. The image sets it to the rollout policy. |
| `GUARDRAIL_MODELS_DIR` | Where the classifier's model folder is |
| `GUARDRAIL_TORCH_THREADS` | Threads for the classifier; by default the container's CPU limit |
| `GUARDRAIL_DATABASE_URL`, or `GUARDRAIL_DATABASE_HOST`, `_PORT`, `_NAME`, `_USER`, `_PASSWORD` | Postgres: versioned policy, the shared input store, the decision table, the admin API |
| `GUARDRAIL_DATABASE_SSL` | `require` (default) or `disable` |
| `GUARDRAIL_POLICY_POLL_SECONDS` | How often a replica looks for a newly activated policy version (5) |
| `GUARDRAIL_DECISION_RETENTION_DAYS` | How long decision rows are kept (365; never under 180) |
| `GUARDRAIL_AUTH_TENANT_ID`, `GUARDRAIL_AUTH_AUDIENCE`, `GUARDRAIL_ADMIN_ROLE`, `GUARDRAIL_REVIEWER_ROLE` | Entra ID validation for the admin API. Unset means every admin request is a 503. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME` | Where OpenTelemetry spans go. Unset means no tracing. |

No secret belongs in the policy file, the image or the repository. The database password in
`docker-compose.yml` is for the local stack only.

## What it does not do

- It does not call a model, so anything that needs judgement beyond rules and one classifier is
  left to the model's own instructions, and checked afterwards where it can be.
- It cannot tell hidden text from visible text. By the time text reaches it, styles and PDF
  render modes are gone. That has to be closed where documents are turned into text.
- It does not know whose data an excerpt is. Cross-customer exposure in chat is an access-control
  question for retrieval.
- The check endpoints are unauthenticated and expected to sit behind internal ingress. Do not put
  this service on the public internet as it is.
- LLM-based filters are not built: there is no model deployment for the guardrail yet. They are
  absent, not stubbed.
