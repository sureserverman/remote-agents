"""Closed, schema-validated profiles for the only supported interactive agents."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId


class ProfileError(ValueError):
    """Raised when profile data is not one of the reviewed fixed definitions."""


_EXPECTED_LAUNCHES: dict[str, tuple[str, tuple[str, ...]]] = {
    "claude": ("claude", ("claude",)),
    "claude-remote": ("claude", ("claude", "--remote-control", "{managed_name}")),
    "codex": ("codex", ("codex",)),
    "opencode": ("opencode", ("opencode",)),
    "cursor-agent": ("cursor-agent", ("cursor-agent",)),
}
_GRACEFUL_KEYS = {
    "claude": ("/exit", "Enter"),
    "claude-remote": ("/exit", "Enter"),
    "codex": ("/exit", "Enter", "Enter"),
    "opencode": ("C-c",),
    "cursor-agent": ("/quit", "Enter", "Enter"),
}
_RESUME_ARGUMENTS = {
    "claude": ("--resume",),
    "codex": ("resume",),
    "opencode": ("--session",),
}


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """A reviewed agent command whose arguments never come from Telegram input."""

    profile_id: ProfileId
    executable: str
    launch_argv: tuple[str, ...]
    version_argv: tuple[str, ...]
    graceful_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        expected = _EXPECTED_LAUNCHES.get(str(self.profile_id))
        if expected is None or (self.executable, self.launch_argv) != expected:
            raise ProfileError("profile executable and launch argv must be curated exactly")
        if (
            self.version_argv != ("--version",)
            or self.graceful_keys != _GRACEFUL_KEYS[str(self.profile_id)]
        ):
            raise ProfileError("profile probe and graceful stop policy must be curated exactly")

    @property
    def version_command(self) -> tuple[str, ...]:
        """Return the fixed executable probe command, without user-controlled arguments."""
        return (self.executable, *self.version_argv)

    def resume_argv(self, source_id: ProviderConversationId) -> tuple[str, ...]:
        """Construct one provider-owned resume argv from an internal resolved source only."""
        arguments = _RESUME_ARGUMENTS.get(str(self.profile_id))
        if arguments is None:
            raise ProfileError("profile has no qualified selected-resume command")
        return (self.executable, *arguments, source_id.value)


@dataclass(frozen=True, slots=True)
class ProfileCompatibility:
    """Non-secret installed-agent availability and diagnostic version evidence."""

    profile_id: ProfileId
    available: bool
    version: str | None
    status: str
    reason: str | None


def closed_profiles() -> tuple[ProfileDefinition, ...]:
    """Return all and only the five reviewed profiles in stable UI order."""
    return tuple(
        ProfileDefinition(
            ProfileId(profile_id), executable, argv, ("--version",), _GRACEFUL_KEYS[profile_id]
        )
        for profile_id, (executable, argv) in _EXPECTED_LAUNCHES.items()
    )
