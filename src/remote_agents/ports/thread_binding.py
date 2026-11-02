"""The service's side of a thread binding: which launches want one, and whether it came."""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum
from typing import Protocol


class PendingBinding(StrEnum):
    """Where a session's binding stands, as its pending file says."""

    NONE = "none"
    """No file: the launch never asked, or the nonce has been spent."""
    WANTED = "wanted"
    """The launch asked, and nothing has been typed."""
    TRIED = "tried"
    """A nonce was minted for it. A service that finds this after a restart tries once more."""
    RETRIED = "retried"
    """A nonce was minted for the second try. No third."""
    GAVE_UP = "gave_up"
    UNTRUSTED = "untrusted"
    """The files are there and cannot be trusted. Nothing is typed."""


class ThreadBindings(Protocol):
    def request(self, session_id: str) -> None: ...
    def state(self, session_id: str) -> PendingBinding: ...
    def mint(self, session_id: str, *, retry: bool = False) -> str: ...
    def give_up(self, session_id: str) -> None: ...
    def prune(self, live_sessions: Collection[str]) -> None: ...
    def names(self, session_id: str, hook_name: str) -> bool: ...
