"""Who may change the policy: Entra ID access tokens, validated the way the backend validates them.

Open question Q1, approved: an Entra ID token for this service, the app role `Guardrails.Admin`,
and the token's `oid` as the actor that is written to the audit trail.

FAIL CLOSED, with no development bypass (the backend has none either). If the tenant or audience
is not configured, every admin request is 503: a misconfigured deployment is loudly broken, not
quietly open. A missing, malformed, expired or wrongly signed token is 401. A valid token without
the role is 403.

What is validated: the signature (RS256, against the tenant's published keys), `aud`, `iss` and
`exp`. An operator with database access and no token uses the command line instead
(`python -m guardrail.policy_admin`); that path is guarded by the database's own credentials.

    GUARDRAIL_AUTH_TENANT_ID    the Entra tenant
    GUARDRAIL_AUTH_AUDIENCE     this service's application id URI or client id
    GUARDRAIL_ADMIN_ROLE        Guardrails.Admin
    GUARDRAIL_REVIEWER_ROLE     Guardrails.Reviewer (may record feedback; an admin may too)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("guardrail")
_bearer = HTTPBearer(auto_error=False)
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


@dataclass(frozen=True)
class Caller:
    id: str                         # the token's `oid`: stable, and what the audit trail records
    roles: tuple[str, ...]


def _settings() -> tuple[str, str]:
    return os.environ.get("GUARDRAIL_AUTH_TENANT_ID", ""), os.environ.get("GUARDRAIL_AUTH_AUDIENCE", "")


def admin_role() -> str:
    return os.environ.get("GUARDRAIL_ADMIN_ROLE", "Guardrails.Admin")


def reviewer_role() -> str:
    return os.environ.get("GUARDRAIL_REVIEWER_ROLE", "Guardrails.Reviewer")


def signing_key(token: str, tenant: str):
    """The key this token says it was signed with, from the tenant's published keys (cached)."""
    client = _jwks_clients.get(tenant)
    if client is None:
        client = _jwks_clients[tenant] = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", cache_keys=True)
    return client.get_signing_key_from_jwt(token).key


def caller(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Caller:
    tenant, audience = _settings()
    if not tenant or not audience:
        log.error("admin authentication is not configured; refusing the request rather than serving it open")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Admin authentication is not configured on this server.")
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token.", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(credentials.credentials, signing_key(credentials.credentials, tenant), algorithms=["RS256"],
                            audience=audience, issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
                            options={"require": ["exp", "aud", "iss"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token has expired.",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
        log.warning("an admin token was refused: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid access token.",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    identity = claims.get("oid") or claims.get("sub")
    if not identity:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "The token names no caller.")
    roles = claims.get("roles") or []
    return Caller(str(identity), tuple(str(role) for role in roles) if isinstance(roles, list) else ())


def admin(who: Caller = Depends(caller)) -> Caller:
    if admin_role() not in who.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This needs the {admin_role()} role.")
    return who


def reviewer(who: Caller = Depends(caller)) -> Caller:
    if admin_role() not in who.roles and reviewer_role() not in who.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This needs the {reviewer_role()} role.")
    return who
