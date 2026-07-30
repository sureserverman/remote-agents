"""Fail-closed, content-free authorization at the Telegram boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthorizationUpdate:
    """Minimal metadata needed to authorize before reading update content or callback data."""

    sender_id: int | None
    chat_id: int | None
    chat_type: str | None
    kind: str


class ContentFreeDenialLog:
    """Bounded denial counter that never stores sender, chat, update, or callback content."""

    def __init__(self, *, limit: int = 10) -> None:
        self._limit = limit
        self._events: list[str] = []

    def record(self) -> None:
        if len(self._events) < self._limit:
            self._events.append("denied")

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(self._events)


class AuthorizationGate:
    """Invoke parsing only after the configured owner and private chat both match exactly."""

    def __init__(
        self, owner_user_id: int, owner_chat_id: int, denials: ContentFreeDenialLog
    ) -> None:
        self._owner_user_id = owner_user_id
        self._owner_chat_id = owner_chat_id
        self._denials = denials

    def dispatch(self, update: AuthorizationUpdate, parse_and_handle: Callable[[], None]) -> bool:
        if (
            update.kind != "callback"
            or update.sender_id != self._owner_user_id
            or update.chat_id != self._owner_chat_id
            or update.chat_type != "private"
        ):
            self._denials.record()
            return False
        parse_and_handle()
        return True
