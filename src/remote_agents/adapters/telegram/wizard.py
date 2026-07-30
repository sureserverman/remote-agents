"""Closed-profile choice and display-only label state for the Telegram launch wizard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from remote_agents.adapters.telegram.callbacks import CallbackStateStore

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


@dataclass(frozen=True, slots=True)
class ProfileChoice:
    label: str
    enabled: bool
    callback_token: str | None
    reason: str | None


class LaunchWizard:
    """Keep profile selection and labels local until a later confirmation task submits them."""

    def __init__(
        self,
        profiles: Callable[[], tuple[ProfileAvailability, ...]],
        callbacks: CallbackStateStore,
        *,
        label_limit: int = 40,
    ) -> None:
        if label_limit < 1 or label_limit > 40:
            raise ValueError("label limit must be between 1 and 40")
        self._profiles = profiles
        self._callbacks = callbacks
        self._label_limit = label_limit
        self._selected_profile: str | None = None
        self._label: str | None = None

    @property
    def selected_profile(self) -> str | None:
        return self._selected_profile

    @property
    def label(self) -> str | None:
        return self._label

    def profile_choices(
        self, *, owner_id: int, chat_id: int, view_revision: int
    ) -> tuple[ProfileChoice, ...]:
        """Render fixed profile labels, with opaque callback state only for live profiles."""

        available = {profile.profile_id: profile for profile in self._profiles()}
        return tuple(
            self._choice(available.get(profile_id), owner_id, chat_id, view_revision)
            for profile_id in _PROFILE_LABELS
            if profile_id in available
        )

    def select_profile(
        self, token: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> str | None:
        """Resolve then recheck availability, rejecting stale or newly disabled choices."""

        state = self._callbacks.resolve(
            token,
            owner_id=owner_id,
            chat_id=chat_id,
            view_revision=view_revision,
        )
        if state is None or state.action != "profile.select":
            return None
        profile = next(
            (profile for profile in self._profiles() if profile.profile_id == state.entity_id),
            None,
        )
        if profile is None or not profile.available:
            return None
        self._selected_profile = profile.profile_id
        return profile.profile_id

    def set_label(self, label: str | None) -> str | None:
        """Accept an optional bounded, printable display label; duplicates remain valid."""

        if label is None:
            self._label = None
            return None
        if any(not character.isprintable() for character in label):
            raise ValueError("label must be a bounded printable display value")
        normalized = " ".join(label.split())
        if not normalized:
            self._label = None
            return None
        if len(normalized) > self._label_limit:
            raise ValueError("label must be a bounded printable display value")
        self._label = normalized
        return normalized

    def back(self) -> None:
        """Return to project choice without retaining a selected profile."""

        self._selected_profile = None

    def cancel(self) -> None:
        """Discard all local wizard state without changing any application resource."""

        self._selected_profile = None
        self._label = None

    def _choice(
        self,
        profile: ProfileAvailability | None,
        owner_id: int,
        chat_id: int,
        view_revision: int,
    ) -> ProfileChoice:
        if profile is None:
            raise ValueError("profile availability must include only curated profiles")
        if not profile.available:
            return ProfileChoice(_PROFILE_LABELS[profile.profile_id], False, None, profile.reason)
        return ProfileChoice(
            _PROFILE_LABELS[profile.profile_id],
            True,
            self._callbacks.create(
                "profile.select", profile.profile_id, owner_id, chat_id, view_revision
            ),
            None,
        )
