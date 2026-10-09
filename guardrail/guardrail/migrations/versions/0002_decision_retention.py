"""Decision rows can leave through the retention purge, and through nothing else.

Open question Q45, decided by the owner on 3 Oct 2026: keep decision rows for 365 days
(configurable), purge daily, no partitions at this volume.

Until now `decision` and `feedback` rejected every UPDATE and DELETE. They still reject every
UPDATE. A DELETE is accepted only inside a transaction that has set `guardrail.retention_purge` to
`on`, which is what `guardrail.decisions_db.purge` does and nothing else in the code does. The
setting is local to that transaction. This guards against the application, or a hand at a prompt,
deleting audit rows by accident; it is not a defence against someone with database rights who
means to.

Uses nothing newer than PostgreSQL 13.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE FUNCTION guardrail.append_only_until_purged() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' AND current_setting('guardrail.retention_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION '% rows cannot be changed, and are deleted only by the retention purge', TG_TABLE_NAME;
    END;
    $$
    """,
    "DROP TRIGGER decision_append_only ON guardrail.decision",
    """
    CREATE TRIGGER decision_append_only BEFORE UPDATE OR DELETE ON guardrail.decision
        FOR EACH ROW EXECUTE FUNCTION guardrail.append_only_until_purged()
    """,
    "DROP TRIGGER feedback_append_only ON guardrail.feedback",
    """
    CREATE TRIGGER feedback_append_only BEFORE UPDATE OR DELETE ON guardrail.feedback
        FOR EACH ROW EXECUTE FUNCTION guardrail.append_only_until_purged()
    """,
]

DOWNGRADE = [
    "DROP TRIGGER feedback_append_only ON guardrail.feedback",
    """
    CREATE TRIGGER feedback_append_only BEFORE UPDATE OR DELETE ON guardrail.feedback
        FOR EACH ROW EXECUTE FUNCTION guardrail.reject_change()
    """,
    "DROP TRIGGER decision_append_only ON guardrail.decision",
    """
    CREATE TRIGGER decision_append_only BEFORE UPDATE OR DELETE ON guardrail.decision
        FOR EACH ROW EXECUTE FUNCTION guardrail.reject_change()
    """,
    "DROP FUNCTION guardrail.append_only_until_purged()",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
