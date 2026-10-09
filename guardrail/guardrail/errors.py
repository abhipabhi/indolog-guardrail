"""Exceptions shared by modules that must not import each other."""
from __future__ import annotations


class NotReady(RuntimeError):
    """A valid policy that this service cannot put in force: something an ENFORCED check needs (its
    model) is not there. Raised by a `prepare` or `ready` hook; whoever catches it keeps the last
    good policy. The message is shown on /health and in the admin API's answer, so it names the
    check and the kind of error and never a path."""
