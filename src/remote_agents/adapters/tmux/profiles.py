"""Local executable/version checks for closed profile definitions."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from remote_agents.adapters.tmux.runtime import LaunchProfile
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import (
    ProfileCompatibility,
    ProfileDefinition,
    ProfileError,
)
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE
from remote_agents.ports.terminal_text import probe_version_line

_RESUME_ARGUMENTS = {
    "claude": ("--resume",),
    "codex": ("resume",),
    "opencode": ("--session",),
}

_READINESS_MARKERS = {
    "claude": "Claude Code",
    "claude-remote": "Claude Code",
    "codex": "Codex",
    "opencode": "Ask anything...",
    "cursor-agent": "Cursor",
}
#: What each agent prints while it is stopped on a question rather than working.
#:
#: These used to mean only "not ready yet", and were consulted to decide whether to *keep
#: waiting*. They now **answer** the launch: an agent that has drawn its folder-trust
#: question has finished starting and will not start further, so the first capture carrying
#: one of these is the conclusion rather than a reason to poll on to the end of the budget.
#:
#: `claude`'s entry is the *pre-trust* screen rather than the dialog itself, which is why
#: `TmuxTerminal._is_awaiting_trust` also classifies the capture for the profiles that can be
#: asked -- a pane resting on the question matches nothing here.
_READINESS_BLOCKERS = {
    "claude": ("Accessing workspace:",),
    "claude-remote": ("Accessing workspace:",),
    "codex": ("Do you trust the contents of this directory?",),
    "cursor-agent": ("Workspace Trust Required",),
}


#: How long after its readiness marker each agent may still raise a folder-trust dialog.
#:
#: **Measured 2026-09-08, in wall-clock** -- see
#: `docs/acceptance-2026-09-08-untrusted-launch.md` section 1. Sample counts are not used:
#: each poll costs a tmux round-trip as well as its sleep, so converting a sample index at the
#: sleep interval understates real elapsed time.
#:
#: `codex` is the entry that earns the mechanism. Its banner and its dialog are **0.081-0.084 s
#: apart** across five launches, and its banner is its readiness marker -- so a launch whose
#: deciding capture lands in that window reports a ready agent that is about to stop on a
#: question. 0.1 s is the measured maximum plus one poll interval (0.01 s), rounded up.
#:
#: `claude` and `claude-remote` are 0.0 because ten launches produced **no dialog at all** on
#: this host, which makes their gap *undefined* rather than zero. The number is a floor chosen
#: in the absence of the race, not a measurement of it, and the acceptance document says so; a
#: host that does raise the dialog is where to re-measure.
#:
#: `opencode` and `cursor-agent` are absent rather than zero, because an unmeasured agent and
#: an agent measured at zero are different things and the `.get` default is where the first
#: belongs.
_TRUST_SETTLE_SECONDS = {
    "claude": 0.0,
    "claude-remote": 0.0,
    "codex": 0.1,
}


def probe_profiles(
    profiles: tuple[ProfileDefinition, ...],
    *,
    resolve: Callable[[str], Path | None] | None = None,
    run_version: Callable[[tuple[str, ...]], str] | None = None,
) -> tuple[ProfileCompatibility, ...]:
    """Probe each fixed profile independently without launching an interactive agent."""
    resolve = _resolve_executable if resolve is None else resolve
    run_version = _run_version if run_version is None else run_version
    results: list[ProfileCompatibility] = []
    for profile in profiles:
        path = resolve(profile.executable)
        if path is None:
            results.append(
                ProfileCompatibility(
                    profile.profile_id, False, None, "BLOCKED", "executable_missing"
                )
            )
            continue
        try:
            printed = run_version((str(path), *profile.version_argv))
        except (OSError, subprocess.SubprocessError):
            printed = None
        version = None if printed is None else probe_version_line(printed)
        if version is None:
            results.append(
                ProfileCompatibility(
                    profile.profile_id, True, None, "AVAILABLE", "version_probe_failed"
                )
            )
            continue
        results.append(ProfileCompatibility(profile.profile_id, True, version, "AVAILABLE", None))
    return tuple(results)


def _resolve_executable(executable: str) -> Path | None:
    resolved = shutil.which(executable)
    return Path(resolved) if resolved is not None else None


def _run_version(argv: tuple[str, ...]) -> str:
    executable_directory = str(Path(argv[0]).parent)
    environment = os.environ | {
        "PATH": f"{executable_directory}:{os.environ.get('PATH', '')}".rstrip(":")
    }
    completed = subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        env=environment,
    )
    return completed.stdout


def build_launch_profile(
    definition: ProfileDefinition,
    executable: Path,
    session_id: SessionId,
    environment: dict[str, str],
) -> LaunchProfile:
    """Resolve a reviewed definition into a fixed tmux profile for one opaque session."""
    if not executable.is_absolute():
        raise ValueError("profile executable must be absolute")
    argv = tuple(
        str(executable)
        if index == 0
        else f"ra-{session_id}"
        if argument == "{managed_name}"
        else argument
        for index, argument in enumerate(definition.launch_argv)
    )
    return LaunchProfile(
        str(executable),
        argv,
        _with_session_identity(environment, session_id),
        _READINESS_MARKERS[str(definition.profile_id)],
        definition.graceful_keys,
        _READINESS_BLOCKERS.get(str(definition.profile_id), ()),
        _TRUST_SETTLE_SECONDS.get(str(definition.profile_id), 0.0),
    )


def _with_session_identity(environment: dict[str, str], session_id: SessionId) -> dict[str, str]:
    """Name the session in its own environment, without writing into the shared curated one.

    `bootstrap._local_runtime` builds one allowed-environment mapping and closes over it for
    every profile factory, so mutating it would leak one session's identity into the next
    launch. A copy per profile is what keeps the variable per-session.
    """
    return environment | {SESSION_ID_VARIABLE: str(session_id)}


def build_resume_profile(
    definition: ProfileDefinition,
    executable: Path,
    session_id: SessionId,
    source_id: ProviderConversationId,
    environment: dict[str, str],
) -> LaunchProfile:
    """Resolve only a curated provider resume argv into a managed launch profile."""
    if not executable.is_absolute():
        raise ValueError("profile executable must be absolute")
    arguments = _RESUME_ARGUMENTS.get(str(definition.profile_id))
    if arguments is None:
        raise ProfileError("profile has no qualified selected-resume command")
    argv = (str(executable), *arguments, source_id.value)
    return LaunchProfile(
        str(executable),
        argv,
        _with_session_identity(environment, session_id),
        # A resumed agent never reprints the banner in _READINESS_MARKERS, so requiring one
        # here marked every resumed session failed once its startup window elapsed, while
        # its pane carried on working. Blockers still apply: those are drawn on resume too.
        None,
        definition.graceful_keys,
        _READINESS_BLOCKERS.get(str(definition.profile_id), ()),
        # Read from the same table as the launch construction. A resumed profile carries no
        # marker at all, so any live pane counts as ready -- which makes the settle worth
        # strictly more here than at launch, and a second table is how the more exposed of
        # the two would keep the default forever.
        _TRUST_SETTLE_SECONDS.get(str(definition.profile_id), 0.0),
    )
