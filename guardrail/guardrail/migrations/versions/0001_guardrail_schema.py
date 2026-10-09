"""The guardrail's tables: policy versions and their audit trail, what each input check saw,
every decision, and reviewer feedback.

Uses nothing newer than PostgreSQL 13 (the floor `feature/guardrails` set), so the major version of
the deployed server only changes the pin in the compose file.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = [
    # --- policy -------------------------------------------------------------------------------------
    # A version is immutable: its document is written once. Changing anything means a new version.
    # Exactly one version is active; activating an older one is a rollback.
    """
    CREATE TABLE guardrail.policy_version (
        id            BIGSERIAL   PRIMARY KEY,
        label         TEXT        NOT NULL UNIQUE,
        document      JSONB       NOT NULL,
        document_hash TEXT        NOT NULL,
        note          TEXT        NOT NULL DEFAULT '',
        status        TEXT        NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
        created_by    TEXT        NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        activated_by  TEXT,
        activated_at  TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX policy_version_one_active ON guardrail.policy_version ((true)) WHERE status = 'active'",
    """
    CREATE FUNCTION guardrail.policy_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'policy versions cannot be deleted';
        END IF;
        IF NEW.label IS DISTINCT FROM OLD.label OR NEW.document IS DISTINCT FROM OLD.document
           OR NEW.document_hash IS DISTINCT FROM OLD.document_hash OR NEW.note IS DISTINCT FROM OLD.note
           OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'a policy version is immutable; create a new version instead';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER policy_version_immutable BEFORE UPDATE OR DELETE ON guardrail.policy_version
        FOR EACH ROW EXECUTE FUNCTION guardrail.policy_version_guard()
    """,
    # Who created, activated and retired each version, and every change that was refused.
    """
    CREATE TABLE guardrail.policy_audit (
        id                BIGSERIAL   PRIMARY KEY,
        event             TEXT        NOT NULL CHECK (event IN ('created', 'activated', 'retired', 'rejected')),
        policy_version_id BIGINT      REFERENCES guardrail.policy_version (id),
        actor             TEXT        NOT NULL,
        detail            JSONB       NOT NULL DEFAULT '{}'::jsonb,
        at                TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # --- append-only tables -------------------------------------------------------------------------
    """
    CREATE FUNCTION guardrail.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% rows cannot be changed or deleted', TG_TABLE_NAME;
    END;
    $$
    """,
    """
    CREATE TRIGGER policy_audit_append_only BEFORE UPDATE OR DELETE ON guardrail.policy_audit
        FOR EACH ROW EXECUTE FUNCTION guardrail.reject_change()
    """,
    # --- what each input check saw ------------------------------------------------------------------
    # Normalized text, kept only until it expires, for the output and answer checks of the same
    # request. Write-once per request id: the application never updates a live row.
    """
    CREATE TABLE guardrail.request_context (
        request_id    TEXT        PRIMARY KEY,
        content_hash  TEXT        NOT NULL,
        parts         JSONB       NOT NULL,
        status        TEXT        NOT NULL,
        reasons       JSONB       NOT NULL DEFAULT '[]'::jsonb,
        rules_version TEXT        NOT NULL DEFAULT '',
        stored_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at    TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX request_context_expires ON guardrail.request_context (expires_at)",
    # --- every decision -----------------------------------------------------------------------------
    # One row per stage per request: what the caller was told, what it would have been told, the
    # policy version, and each check's mode, verdict, score and measures. Numbers and ids, no text.
    """
    CREATE TABLE guardrail.decision (
        id             BIGSERIAL   PRIMARY KEY,
        request_id     TEXT        NOT NULL,
        stage          TEXT        NOT NULL CHECK (stage IN ('input', 'output', 'answer')),
        status         TEXT        NOT NULL,
        would_be       TEXT        NOT NULL,
        policy_version TEXT        NOT NULL,
        measures       JSONB       NOT NULL DEFAULT '{}'::jsonb,
        checks         JSONB       NOT NULL,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX decision_request ON guardrail.decision (request_id, stage, id DESC)",
    "CREATE INDEX decision_created ON guardrail.decision (created_at)",
    """
    CREATE TRIGGER decision_append_only BEFORE UPDATE OR DELETE ON guardrail.decision
        FOR EACH ROW EXECUTE FUNCTION guardrail.reject_change()
    """,
    # --- what a reviewer made of a decision ---------------------------------------------------------
    """
    CREATE TABLE guardrail.feedback (
        id          BIGSERIAL   PRIMARY KEY,
        decision_id BIGINT      NOT NULL REFERENCES guardrail.decision (id),
        label       TEXT        NOT NULL CHECK (label IN ('confirm', 'overturn')),
        reviewer    TEXT        NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX feedback_decision ON guardrail.feedback (decision_id)",
    """
    CREATE TRIGGER feedback_append_only BEFORE UPDATE OR DELETE ON guardrail.feedback
        FOR EACH ROW EXECUTE FUNCTION guardrail.reject_change()
    """,
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS guardrail.feedback",
    "DROP TABLE IF EXISTS guardrail.decision",
    "DROP TABLE IF EXISTS guardrail.request_context",
    "DROP TABLE IF EXISTS guardrail.policy_audit",
    "DROP TABLE IF EXISTS guardrail.policy_version",
    "DROP FUNCTION IF EXISTS guardrail.reject_change()",
    "DROP FUNCTION IF EXISTS guardrail.policy_version_guard()",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
