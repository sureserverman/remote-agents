"""Render the user unit at install time, from the interpreter that is doing the installing.

The shipped `systemd/remote-agents.service` is the behavioural reference for this module, and
the one thing it deliberately does not carry across is how it names paths. That file writes
`%h/dev/infra/remote-agents/.venv/bin/remote-agents`, which is two assumptions stacked: that
the supervisor will expand `%h`, and that the checkout sits at a fixed place beneath it.

*The specifier cannot survive.* `%h` is systemd's, and launchd has no equivalent -- a plist
holds strings, expanded by nothing -- so a definition that leans on it is one the other
adapter physically cannot mirror. Making every path absolute at render time is the only rule
that holds on both supervisors, which is why `SupervisorArtifact.path` is documented as
absolute rather than merely happening to be.

*The checkout path cannot survive either.* Dropping `%h` while keeping `dev/infra/remote-agents`
spelled out would keep the same coupling with the indirection removed, and it is the half that
was actually wrong: the venv is wherever the operator installed it, and a pipx or a relocated
checkout was already outside what that line could describe. `Path(sys.executable)` is how this
project names itself in files it writes (`hook_install.agent_event_command`,
`bootstrap._projects_command`), and it is exact by construction -- the interpreter running the
installer is the interpreter the console script belongs to.

There is no `EnvironmentFile=`. It was retired so that exactly one parser reads the credential
file; the service opens it in-process now. This module is a second place it could come back,
and it would come back quietly, since a string built at runtime is not something a gate can
grep the way it greps the shipped file. The contract test is what stands in for that grep.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from remote_agents.ports.service_supervisor import (
    LivenessMeaning,
    SupervisorArtifact,
    SupervisorKind,
)

UNIT_NAME = "remote-agents.service"

#: The service definition, with the three host-specific values left to `str.format`.
#:
#: Every directive below the paths is the shipped unit's, unchanged and for its original
#: reason. `KillMode=process` is the one to be careful with: it is what leaves the managed tmux
#: sessions running when the service that launched them is stopped, so a session survives a
#: restart of its own control plane. Stage 3 drills the launchd analogue of exactly that.
_UNIT_TEMPLATE = """\
[Unit]
Description=Remote Agents private Telegram control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_directory}
ExecStart={executable} serve --config {config_path}
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillMode=process
UMask=0077
NoNewPrivileges=yes
RestrictSUIDSGID=yes
LockPersonality=yes
ProtectControlGroups=yes
ProtectKernelTunables=yes

[Install]
WantedBy=default.target
"""


def _command_word(value: Path) -> str:
    """Render one path as a single `ExecStart` word, quoting only when it would split.

    The shipped unit never needed this because it was written by hand for one known layout. A
    unit rendered from `sys.executable` is written for whatever layout the operator has, and
    systemd splits `ExecStart` on whitespace, so an interpreter beneath a directory with a
    space in its name would silently become two arguments. Quoting is systemd's own: double
    quotes around the word, with `\\` and `"` escaped inside it.
    """
    # `%` first, and before anything else looks at the text. Specifier expansion is a
    # *separate* parsing stage from word-splitting and quoting, and quotes do not protect a
    # specifier: `%h` inside a quoted word still expands. So a literal `%` in a real directory
    # name -- unusual but perfectly legal -- would otherwise be read as the start of a
    # specifier and silently rewrite the path. `%%` is systemd's own escape for a literal one.
    text = str(value).replace("%", "%%")
    if not any(character in text for character in ' \t"\\'):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True, slots=True)
class SystemdSupervisor:
    """`ServiceSupervisor` for a systemd **user** manager, rendering its own unit.

    Both inputs are constructor parameters with host defaults rather than reads of the process
    environment, so a test can render a unit that depends on nothing about the machine running
    it -- the same property that lets the launchd adapter be exercised on Linux.
    """

    interpreter: Path = field(default_factory=lambda: Path(sys.executable))
    home: Path = field(default_factory=Path.home)

    kind: ClassVar[SupervisorKind] = SupervisorKind.SYSTEMD

    #: `is-active` is false for a unit that is loaded but not running, so zero means running.
    liveness_meaning: ClassVar[LivenessMeaning] = LivenessMeaning.RUNNING

    def __post_init__(self) -> None:
        """Enforce the absolute-path invariant every docstring here asserts.

        `ProductionPaths.for_home` refuses a relative home with a `ConfigError`; this is the
        same rule, applied where the *artifacts* are rendered. Upheld by convention it was
        upheld by nothing: the wired path only ever passes `Path.home()`, so a relative home
        would arrive from a test fixture or a future caller and render a definition full of
        relative paths that the supervisor resolves against its own working directory --
        silently, and differently on each platform. `ValueError` rather than `ConfigError`
        because an adapter may not import `remote_agents.config` under ARCH-02.
        """
        if not self.home.is_absolute():
            raise ValueError(f"supervisor home must be absolute: {self.home}")
        if not self.interpreter.is_absolute():
            raise ValueError(f"supervisor interpreter must be absolute: {self.interpreter}")

    @property
    def unit_path(self) -> Path:
        """Where the user manager reads unit files from, spelled out rather than deferred."""
        return self.home / ".config" / "systemd" / "user" / UNIT_NAME

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        """The one file this version installs: the user unit, fully rendered."""
        content = _UNIT_TEMPLATE.format(
            working_directory=_command_word(self.home),
            executable=_command_word(self.interpreter.parent / "remote-agents"),
            config_path=_command_word(self.home / ".config" / "remote-agents" / "config.toml"),
        )
        return (SupervisorArtifact(path=self.unit_path, content=content),)

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        """Nothing yet -- and the empty tuple is the honest answer, not an unfinished one.

        DEC-051's rule is that an artifact leaves `artifacts()` by *moving* here rather than by
        disappearing, so that a path no current version installs is still a path every current
        version can take away. That rule has had no occasion to fire on this side: this adapter
        installs to `~/.config/systemd/user/remote-agents.service`, which is the same path the
        shipped static unit was copied to, so upgrading to a generated unit overwrites the file
        it replaces instead of stranding one beside it.

        Inventing an entry to look complete would be worse than empty: `artifact_paths_to_remove`
        feeds an uninstaller, and a path named here is a path something will try to delete.
        """
        return ()

    def install_command(self) -> tuple[str, ...]:
        """Register the written unit and bring it up, as the runbook already documents."""
        return ("systemctl", "--user", "enable", "--now", UNIT_NAME)

    def remove_command(self) -> tuple[str, ...]:
        """Unregister and stop. Deleting the file is `artifact_paths_to_remove`'s answer."""
        return ("systemctl", "--user", "disable", "--now", UNIT_NAME)

    def start_command(self) -> tuple[str, ...]:
        """Start an already-registered unit, without re-enabling it to get there."""
        return ("systemctl", "--user", "start", UNIT_NAME)

    def liveness_command(self) -> tuple[str, ...]:
        """`is-active --quiet`: exit status only, with `--quiet` making that explicit.

        The port forbids a caller reading this command's output, and `--quiet` is that same
        rule expressed to systemd -- there is no output to be tempted by. It is the argv
        `bootstrap`'s `doctor` already runs inline; Task 2.4 is what routes it through here.
        """
        return ("systemctl", "--user", "is-active", "--quiet", UNIT_NAME)
