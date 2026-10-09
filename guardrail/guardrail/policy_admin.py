"""The operator's command line for the guardrail's database. For someone with database access and no admin token.

    python -m guardrail.policy_admin migrate                              # create or upgrade the tables
    python -m guardrail.policy_admin ensure rules.rollout.json --actor NAME   # if nothing is active yet, seed this file
    python -m guardrail.policy_admin seed rules.rollout.json --actor NAME # store a file as a version and activate it
    python -m guardrail.policy_admin create FILE --actor NAME [--note ..] # store a file as a draft version
    python -m guardrail.policy_admin activate ID --actor NAME             # put a version in force (older = rollback)
    python -m guardrail.policy_admin list
    python -m guardrail.policy_admin purge                                # remove expired input records, and decision
                                                                          # rows past their retention period

The database comes from the environment, as for the service (guardrail/db.py). Every change made
here is validated and audited exactly as one made through the admin API; --actor is what the audit
trail records, so give your own name.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import classifier, decisions_db
from .db import engine_from_env
from .policy_db import PolicyAdmin, PolicyError
from .sources_db import PostgresSourceStore

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def migrate(engine, revision: str = "head") -> None:
    """Creates or upgrades the guardrail's tables. Safe to run again: it does only what is missing."""
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["engine"] = engine
    command.upgrade(config, revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m guardrail.policy_admin", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    for name in ("seed", "create", "ensure"):
        sub = commands.add_parser(name)
        sub.add_argument("file", type=Path)
        sub.add_argument("--actor", required=True)
        sub.add_argument("--note", default="")
    sub = commands.add_parser("activate")
    sub.add_argument("id", type=int)
    sub.add_argument("--actor", required=True)
    commands.add_parser("list")
    commands.add_parser("purge")
    args = parser.parse_args(argv)

    engine = engine_from_env()
    # The same check the service makes: a version whose enforced check cannot load its model
    # where this runs is refused. Run it where the model is: in the service's image.
    policies = PolicyAdmin(engine, ready=classifier.prepare)
    try:
        if args.command == "migrate":
            migrate(engine)
            print("the guardrail schema is up to date")
        elif args.command == "ensure":
            active = policies.active()
            if active is not None:
                print(f"version {active.id} ({active.label}) is already active; nothing to do")
            else:
                document = json.loads(args.file.read_text(encoding="utf-8"))
                version = policies.create(document, actor=args.actor, note=args.note or f"first policy, from {args.file.name}")
                policies.activate(version.id, actor=args.actor)
                print(f"no version was active; created and activated version {version.id} ({version.label})")
        elif args.command in ("seed", "create"):
            document = json.loads(args.file.read_text(encoding="utf-8"))
            version = policies.create(document, actor=args.actor, note=args.note or f"from {args.file.name}")
            print(f"created version {version.id} ({version.label}), status {version.status}")
            if args.command == "seed":
                version = policies.activate(version.id, actor=args.actor)
                print(f"activated version {version.id} ({version.label})")
        elif args.command == "activate":
            version = policies.activate(args.id, actor=args.actor)
            print(f"version {version.id} ({version.label}) is active")
        elif args.command == "list":
            for version in policies.versions():
                print(f"{version.id:>4}  {version.status:8}  {version.label:20}  created by {version.created_by} "
                      f"{version.created_at:%Y-%m-%d %H:%M}  {version.note}")
        elif args.command == "purge":
            print(f"removed {PostgresSourceStore(engine).purge()} expired input record(s)")
            decisions, feedback = decisions_db.purge(engine)
            print(f"removed {decisions} decision row(s) older than {decisions_db.retention_days()} days, "
                  f"and {feedback} feedback row(s) on them")
    except PolicyError as error:
        print(f"refused ({error.code}): {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
