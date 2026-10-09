"""Who may open the app.

The guardrail's check endpoints have no sign-in of their own, and this app makes them work for
whoever can open it. So the app is for one machine by default, and for a LAN only with a shared
password:

  * Bound to this machine (127.0.0.1 or localhost): no password needed, though one is honoured.
  * Reachable from anywhere else: the app refuses to serve unless CHATBOT_PASSWORD holds a shared
    password of at least 12 characters, and every visitor must enter it.

This is for testing on a trusted network. It is not a sign-in system, and the app must never be
put on the public internet.
"""
from __future__ import annotations

import hmac

from app.config import LOOPBACK, Settings

MIN_PASSWORD = 12


def reachable_on(server_address: str | None) -> str:
    """The address the app listens on. Streamlit listens on every interface when none is set."""
    return (server_address or "").strip() or "0.0.0.0"


def refusal(settings: Settings, server_address: str | None) -> str | None:
    """Why the app will not serve at all, or None."""
    where = reachable_on(server_address)
    if where not in LOOPBACK and len(settings.password) < MIN_PASSWORD:
        return (f"This app can be reached from other machines (it is bound to {where}) and no shared password of at "
                f"least {MIN_PASSWORD} characters is set. Set CHATBOT_PASSWORD, or bind it to localhost.")
    return None


def password_wanted(settings: Settings) -> bool:
    return bool(settings.password)


def password_matches(settings: Settings, given: str) -> bool:
    return bool(settings.password) and hmac.compare_digest(given.encode("utf-8"), settings.password.encode("utf-8"))
