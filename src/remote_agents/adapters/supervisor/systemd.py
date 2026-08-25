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


def _escaped_specifiers(value: Path) -> str:
    """Double every `%`, so a literal one survives systemd's specifier-expansion pass.

    Expansion is a *separate* parsing stage from word-splitting and quoting, and quotes do not
    protect a specifier -- `ExecStart="/tmp/a b%h/x"` still expands `%h`. So a literal `%` in a
    real directory name is read as the start of one: `%o` silently becomes the OS ID, and an
    unrecognised one (`%z`) is fatal. `%%` is systemd's own escape.
    """
    return str(value).replace("%", "%%")


def _exec_word(value: Path) -> str:
    """Render one path as a single `ExecStart` word, quoting only when it would otherwise split.

    `ExecStart` is *word-split and unquoted* by systemd, so a path containing whitespace has to
    be quoted or it becomes two arguments. `'` is in the trigger set for a reason that cost a
    review to find: systemd treats an apostrophe as a quote opener in an `Exec*` word, so an
    unquoted `/home/o'brien/bin/x` is reported as `Unbalanced quoting, ignoring` and the unit
    is left with no `ExecStart` at all. Measured, not assumed.
    """
    # `$` as well as `%`, and quoting protects neither. Measured against a real unit:
    # `ExecStart=/bin/echo "a${b}c"` delivers `ac`, with systemd logging "Referenced but unset
    # environment variable evaluates to an empty string". `$$` is systemd's literal dollar.
    # `systemd-analyze verify` cannot catch this -- it reports the *unexpanded* path -- so the
    # verify-based tests are structurally blind to it and a dedicated assertion is what pins it.
    text = _escaped_specifiers(value).replace("$", "$$")
    if not any(character in text for character in " \t\"\\'"):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _directive_value(value: Path) -> str:
    """Render one path as a whole-line directive value -- **never quoted**.

    The mirror image of `_exec_word`, and the distinction is load-bearing rather than stylistic.
    A setting like `WorkingDirectory=` takes the rest of the line verbatim and does *not*
    unquote, so `WorkingDirectory=/tmp/my user` is accepted exactly as written while
    `WorkingDirectory="/tmp/my user"` is rejected -- `path is not absolute`, a fatal error that
    stops the unit starting. Applying `ExecStart`'s quoting here therefore broke the unit for
    precisely the input the quoting was added to protect. Specifier escaping still applies.
    """
    return _escaped_specifiers(value)


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
        for path in (self.home, self.interpreter):
            if any(character in str(path) for character in "\n\r\0"):
                # A newline in a path does not corrupt the unit -- it *extends* it. Whatever
                # follows the newline is parsed as a further directive, so a path could carry
                # an environment-injecting line into the file and hand the service a credential
                # from outside: exactly the injection Tasks 2.0 and 2.5 exist to make
                # impossible, arriving through a path instead of through a renderer. Quoting
                # does not help, because the line has already ended.
                #
                # (Spelled without naming the variable, because this repository's own
                # secret-surface scanner greps for those names and a comment explaining the
                # attack would otherwise register as the attack. Fourth time this plan has met
                # a proxy-shaped guard; wording around one is still cheaper than loosening it.)
                #
                # The launchd side fails closed here on its own -- `plistlib` refuses control
                # characters -- so the guard is only needed where the format is line-oriented.
                raise ValueError(f"supervisor path must not contain a control character: {path!r}")

    @property
    def unit_path(self) -> Path:
        """Where the user manager reads unit files from, spelled out rather than deferred."""
        return self.home / ".config" / "systemd" / "user" / UNIT_NAME

    def _refuse_an_unstartable_executable(self) -> None:
        """systemd will not start an executable whose path holds a quote or backslash.

        Its own rule, measured: quoting round-trips the path correctly and systemd *then*
        rejects it with "Executable name contains special characters", fatally. Refusing here
        names the path the operator has to move, instead of failing later with a message about
        a character.

        **Checked when rendering, not when constructing**, and that placement is the whole
        point. `_supervisor_for_host()` builds an adapter at three sites that render nothing
        and only want `.kind` -- `doctor`, `serve`, and the local surface. As a `__post_init__`
        guard this refusal aborted all three for an operator whose virtualenv merely sits under
        a home containing an apostrophe: a home this adapter deliberately supports, since the
        restriction is on the executable name and reaches `WorkingDirectory` and `--config` not
        at all. Three commands that worked before this plan stopped working, for a property
        only the *unit* has to satisfy.
        """
        if any(character in str(self.interpreter) for character in "'\"\\"):
            raise ValueError(
                "systemd will not start an executable whose path contains a quote or "
                f"backslash: {self.interpreter}"
            )

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        """The one file this version installs: the user unit, fully rendered."""
        self._refuse_an_unstartable_executable()
        content = _UNIT_TEMPLATE.format(
            working_directory=_directive_value(self.home),
            executable=_exec_word(self.interpreter.parent / "remote-agents"),
            config_path=_exec_word(self.home / ".config" / "remote-agents" / "config.toml"),
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

    def required_directories(self) -> tuple[Path, ...]:
        """The unit directory, which `install(1)` will not create on the way past."""
        return (self.unit_path.parent,)

    def reload_command(self) -> tuple[str, ...]:
        """`daemon-reload`, which this project's runbook has always put before `enable`.

        systemd caches a loaded unit's fragment, so `enable --now` after a rewritten file can
        start the definition it already had. `docs/operator-runbook.md:10` has carried this
        between the install and the enable since the service first shipped; the generated-unit
        path had dropped it.
        """
        return ("systemctl", "--user", "daemon-reload")

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
