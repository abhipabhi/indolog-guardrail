"""A time budget the checks can see.

The engine gives each check a deadline before it runs. A check that does its work in steps (the
classifier, one batch of windows at a time; the rules, one rule at a time) calls `check()` between
steps and stops with `TimedOut` once the deadline has passed. The engine turns that into a review
with the reason `<check>.timeout`: fail closed, like any other check that could not finish.

This is cooperative. Nothing here can interrupt a check in the middle of a step, so a deadline is
overrun by at most one step.
"""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("guardrail_deadline", default=None)


class TimedOut(RuntimeError):
    """The check's time ran out before it finished."""


@contextmanager
def until(deadline: float | None):
    """Runs the block with this deadline (a time.monotonic() value) in force."""
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def check() -> None:
    """Raises TimedOut if the deadline in force has passed. Does nothing when there is none."""
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimedOut("out of time")
