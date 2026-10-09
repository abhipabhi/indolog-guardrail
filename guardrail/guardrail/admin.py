"""The policy admin API: a separate application, mounted at /guardrail/policy.

Separate on purpose. It has its own OpenAPI document (/guardrail/policy/openapi.json) and its own
authentication, so the check endpoints' contract and their document do not change because policy
administration exists.

Every change is validated, versioned and audited: a new policy is a new immutable version, checked
by the same model that loads rules.json; activating a version (an older one is a rollback) retires
the one in force in the same transaction; who did what is in the audit trail, including changes
that were refused. Nothing here can switch off the fail-closed handling: that is code, not policy.

Needs the database. Without one, every route answers 503.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from . import admin_auth, classifier, decisions_db, tracing
from .admin_auth import Caller
from .policy_db import PolicyAdmin, PolicyError

app = FastAPI(title="IndoLog guardrail policy admin", version="1.0.0", telemetry=tracing.FASTAPI_TELEMETRY, description=(
    "Policy versions for the guardrail. A version is immutable; to change the policy, create a new "
    "version and activate it. Activating an older version is a rollback. Needs an Entra ID access "
    "token with the Guardrails.Admin role."))

# Set by the main application when a database is configured.
state: dict[str, Any] = {"engine": None, "store": None}


def _admin_store() -> PolicyAdmin:
    if state["engine"] is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No policy database is configured on this server.")
    return PolicyAdmin(state["engine"], ready=classifier.prepare)


class NewVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: dict[str, Any] = Field(description="A whole policy, in the shape of rules.json. Its `version` is the new version's label.")
    note: str = Field(default="", max_length=2000, description="Why this version exists. Kept in the audit trail.")


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: int
    label: Literal["confirm", "overturn"]


def _refused(error: PolicyError) -> HTTPException:
    code = {"invalid": status.HTTP_422_UNPROCESSABLE_ENTITY, "duplicate": status.HTTP_409_CONFLICT,
            "not_found": status.HTTP_404_NOT_FOUND, "not_ready": status.HTTP_409_CONFLICT}[error.code]
    return HTTPException(code, {"error": error.code, "message": str(error)})


@app.get("/active", summary="The policy version in force")
def active(who: Caller = Depends(admin_auth.admin), policies: PolicyAdmin = Depends(_admin_store)):
    version = policies.active()
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No version is active; the service is running on its file policy.")
    return {**version.summary(), "document": version.document}


@app.get("/versions", summary="Every version, newest first")
def versions(who: Caller = Depends(admin_auth.admin), policies: PolicyAdmin = Depends(_admin_store)):
    return [version.summary() for version in policies.versions()]


@app.get("/versions/{version_id}", summary="One version, with its document")
def version(version_id: int, who: Caller = Depends(admin_auth.admin), policies: PolicyAdmin = Depends(_admin_store)):
    try:
        found = policies.version(version_id)
    except PolicyError as error:
        raise _refused(error) from None
    return {**found.summary(), "document": found.document}


@app.post("/versions", status_code=status.HTTP_201_CREATED, summary="Create a new version (a draft, not yet in force)")
def create(body: NewVersion, who: Caller = Depends(admin_auth.admin), policies: PolicyAdmin = Depends(_admin_store)):
    try:
        return policies.create(body.document, actor=who.id, note=body.note).summary()
    except PolicyError as error:
        raise _refused(error) from None


@app.post("/versions/{version_id}/activate", summary="Put a version in force (an older one is a rollback)")
def activate(version_id: int, who: Caller = Depends(admin_auth.admin), policies: PolicyAdmin = Depends(_admin_store)):
    try:
        activated = policies.activate(version_id, actor=who.id)
    except PolicyError as error:
        raise _refused(error) from None
    if state["store"] is not None:
        state["store"].refresh()            # this replica now; the others within their poll interval
    return activated.summary()


@app.get("/audit", summary="Who created, activated, retired or was refused what")
def audit(limit: int = 100, who: Caller = Depends(admin_auth.admin), policies: PolicyAdmin = Depends(_admin_store)):
    return policies.audit(max(1, min(limit, 1000)))


@app.get("/decisions/{request_id}", summary="The decisions recorded for one request, with any feedback")
def decisions(request_id: str, who: Caller = Depends(admin_auth.reviewer), policies: PolicyAdmin = Depends(_admin_store)):
    return decisions_db.decisions_for(policies.engine, request_id)


@app.post("/feedback", status_code=status.HTTP_201_CREATED, summary="A reviewer confirms or overturns one decision")
def feedback(body: Feedback, who: Caller = Depends(admin_auth.reviewer), policies: PolicyAdmin = Depends(_admin_store)):
    recorded = decisions_db.record_feedback(policies.engine, body.decision_id, body.label, who.id)
    if recorded is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"There is no decision {body.decision_id}.")
    return {"id": recorded}
