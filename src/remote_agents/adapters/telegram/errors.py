"""Closed, content-free error presentation for the Telegram boundary."""

from __future__ import annotations

from enum import StrEnum

from remote_agents.adapters.telegram.presenters import (
    NavigationCallbacks,
    RenderedMessage,
    render_degraded,
)


class ErrorKind(StrEnum):
    PROFILE_UNAVAILABLE = "profile_unavailable"
    INVALID_PROJECT = "invalid_project"
    REGISTRY_DEGRADED = "registry_degraded"
    CONFLICT = "conflict"
    DUPLICATE_REQUEST = "duplicate_request"
    DATABASE_UNAVAILABLE = "database_unavailable"
    TERMINAL_UNAVAILABLE = "terminal_unavailable"
    UNEXPECTED = "unexpected"


_MESSAGES = {
    ErrorKind.PROFILE_UNAVAILABLE: "This profile is currently unavailable.",
    ErrorKind.INVALID_PROJECT: "This project is no longer available.",
    ErrorKind.REGISTRY_DEGRADED: "The project catalogue is temporarily unavailable.",
    ErrorKind.CONFLICT: "A conflicting operation is already in progress.",
    ErrorKind.DUPLICATE_REQUEST: "This request was already handled.",
    ErrorKind.DATABASE_UNAVAILABLE: "Session storage is temporarily unavailable.",
    ErrorKind.TERMINAL_UNAVAILABLE: "The managed terminal is temporarily unavailable.",
    ErrorKind.UNEXPECTED: "The request could not be completed.",
}


def render_error(
    kind: ErrorKind, callbacks: NavigationCallbacks, *, diagnostic: str | None = None
) -> RenderedMessage:
    """Map a known failure to safe recovery text; diagnostics remain outside Telegram."""

    del diagnostic
    message = _MESSAGES[kind]
    return RenderedMessage(
        text=message,
        keyboard=render_degraded(callbacks).keyboard,
    )
