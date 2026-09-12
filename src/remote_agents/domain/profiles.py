"""Closed, schema-validated profiles for the only supported interactive agents."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.models import ProfileId


class ProfileError(ValueError):
    """Raised when profile data is not one of the reviewed fixed definitions."""


#: Each profile's executable, its ordinary launch argv, and the one remote-control variant
#: it may be launched with instead -- `None` for an agent that has none.
#:
#: **Three columns rather than a second table**, because the third is curated on exactly the
#: same authority as the first two: it is an argv this project executes, and DEC-002 puts
#: every such argv in one reviewed place. A separate mapping would be a second place to
#: remember, which is the shape `TmuxTerminal._trust_dialogs` records having gone wrong.
#:
#: Only `claude` carries a variant. `{managed_name}` is substituted per session by
#: `adapters/tmux/profiles.build_launch_profile`, and is the only substitution any argv here
#: takes.
_EXPECTED_LAUNCHES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...] | None]] = {
    "claude": ("claude", ("claude",), ("claude", "--remote-control", "{managed_name}")),
    "claude-remote": ("claude", ("claude", "--remote-control", "{managed_name}"), None),
    "codex": ("codex", ("codex",), None),
    "opencode": ("opencode", ("opencode",), None),
    "cursor-agent": ("cursor-agent", ("cursor-agent",), None),
}
_GRACEFUL_KEYS = {
    "claude": ("/exit", "Enter"),
    "claude-remote": ("/exit", "Enter"),
    "codex": ("/exit", "Enter", "Enter"),
    "opencode": ("C-c",),
    "cursor-agent": ("/quit", "Enter", "Enter"),
}


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """A reviewed agent command whose arguments never come from Telegram input."""

    profile_id: ProfileId
    executable: str
    launch_argv: tuple[str, ...]
    version_argv: tuple[str, ...]
    graceful_keys: tuple[str, ...]

    remote_control_argv: tuple[str, ...] | None = None
    """The one reviewed argv that starts this agent already under remote control.

    A second argv rather than a second profile. The retired `claude-remote` profile said the
    same thing as an agent of its own, which put the choice in every picker on both surfaces
    and made it a property of the session the owner happened to start. It is not: the answer
    comes from one host-wide setting (`remoteControlAtStartup` in Claude's own settings file),
    read at launch, so what varies between two launches of the same agent is a flag and not
    an identity.

    Defaulted to `None` because that is the correct value for four of the five profiles --
    but **not** a value `claude` may take: `__post_init__` requires the curated variant there,
    so a definition that simply omitted it raises rather than launching unconnected while the
    Settings row reads *on*.
    """

    def __post_init__(self) -> None:
        expected = _EXPECTED_LAUNCHES.get(str(self.profile_id))
        if (
            expected is None
            or (
                self.executable,
                self.launch_argv,
                self.remote_control_argv,
            )
            != expected
        ):
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
            ProfileId(profile_id),
            executable,
            argv,
            ("--version",),
            _GRACEFUL_KEYS[profile_id],
            remote_control_argv,
        )
        for profile_id, (executable, argv, remote_control_argv) in _EXPECTED_LAUNCHES.items()
    )
