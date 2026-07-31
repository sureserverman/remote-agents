"""Closed, schema-validated profiles for the only supported interactive agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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
    "codex": ("/exit", "Enter"),
    "opencode": ("C-c",),
    "cursor-agent": ("C-c",),
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


@dataclass(frozen=True, slots=True)
class ProfileCompatibility:
    """Non-secret probe evidence for a profile that remains disabled until qualified."""

    profile_id: ProfileId
    available: bool
    version: str | None
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProfileQualification:
    """Host-local evidence that one exact profile binary passed the interactive contract."""

    profile_id: ProfileId
    version: str
    qualified_on: date

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 160:
            raise ProfileError("qualified version must be concise non-empty text")


def closed_profiles() -> tuple[ProfileDefinition, ...]:
    """Return all and only the five reviewed profiles in stable UI order."""
    return tuple(
        ProfileDefinition(
            ProfileId(profile_id), executable, argv, ("--version",), _GRACEFUL_KEYS[profile_id]
        )
        for profile_id, (executable, argv) in _EXPECTED_LAUNCHES.items()
    )


def qualified_profiles() -> tuple[ProfileQualification, ...]:
    """Return the current host's reviewed live-profile evidence without credentials or logs."""
    qualified_on = date(2026, 7, 30)
    return (
        ProfileQualification(ProfileId("claude"), "2.1.220 (Claude Code)", qualified_on),
        ProfileQualification(ProfileId("claude-remote"), "2.1.220 (Claude Code)", qualified_on),
        ProfileQualification(ProfileId("codex"), "codex-cli 0.146.0", qualified_on),
        ProfileQualification(ProfileId("opencode"), "1.18.10", qualified_on),
        ProfileQualification(ProfileId("cursor-agent"), "2026.07.23-e383d2b", qualified_on),
    )
