"""Closed, schema-validated profiles for the only supported interactive agents."""

from __future__ import annotations

from dataclasses import dataclass

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
_FORBIDDEN_ARGUMENT_FRAGMENTS = (
    "dangerously-skip",
    "bypass-approvals",
    "--auto",
    "--force",
    "--yolo",
)


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
        if self.version_argv != ("--version",) or self.graceful_keys != ("C-c",):
            raise ProfileError("profile probe and graceful stop policy must be curated exactly")
        if any(
            fragment in argument.casefold()
            for argument in self.launch_argv
            for fragment in _FORBIDDEN_ARGUMENT_FRAGMENTS
        ):
            raise ProfileError("profile launch argv contains a dangerous bypass flag")

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


def closed_profiles() -> tuple[ProfileDefinition, ...]:
    """Return all and only the five reviewed profiles in stable UI order."""
    return tuple(
        ProfileDefinition(ProfileId(profile_id), executable, argv, ("--version",), ("C-c",))
        for profile_id, (executable, argv) in _EXPECTED_LAUNCHES.items()
    )
