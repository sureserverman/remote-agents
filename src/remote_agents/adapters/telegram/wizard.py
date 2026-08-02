"""Closed-profile choice and display-only label state for the Telegram launch wizard."""

from __future__ import annotations

from dataclasses import dataclass

_PROFILE_LABELS = {
    "claude": "Claude",
    "claude-remote": "Claude Remote",
    "codex": "Codex",
    "opencode": "OpenCode",
    "cursor-agent": "Cursor Agent",
}


@dataclass(frozen=True, slots=True)
class ProfileAvailability:
    """Non-secret, curated profile availability visible to the owner."""

    profile_id: str
    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.profile_id not in _PROFILE_LABELS:
            raise ValueError("launch profiles must be curated")
