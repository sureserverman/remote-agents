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
_READINESS_BLOCKERS = {
    "claude": ("Accessing workspace:",),
    "claude-remote": ("Accessing workspace:",),
    "codex": ("Do you trust the contents of this directory?",),
    "cursor-agent": ("Workspace Trust Required",),
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
            version = _sanitize_version(run_version((str(path), *profile.version_argv)))
        except (OSError, subprocess.SubprocessError):
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


def _sanitize_version(value: str) -> str:
    line = next((part.strip() for part in value.splitlines() if part.strip()), "")
    if not line:
        raise OSError("version probe returned no text")
    return "".join(character for character in line if character.isprintable())[:160]


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
    )
