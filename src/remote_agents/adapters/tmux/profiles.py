"""Local executable/version checks for closed profile definitions."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from remote_agents.adapters.tmux.runtime import LaunchProfile
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import (
    ProfileCompatibility,
    ProfileDefinition,
    ProfileQualification,
)

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
}


def probe_profiles(
    profiles: tuple[ProfileDefinition, ...],
    *,
    qualifications: tuple[ProfileQualification, ...] = (),
    resolve: Callable[[str], Path | None] | None = None,
    run_version: Callable[[tuple[str, ...]], str] | None = None,
) -> tuple[ProfileCompatibility, ...]:
    """Probe each fixed profile independently without launching an interactive agent."""
    resolve = _resolve_executable if resolve is None else resolve
    run_version = _run_version if run_version is None else run_version
    qualified = {item.profile_id: item for item in qualifications}
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
            version = _sanitize_version(run_version(profile.version_command))
        except (OSError, subprocess.SubprocessError):
            results.append(
                ProfileCompatibility(
                    profile.profile_id, True, None, "BLOCKED", "version_probe_failed"
                )
            )
            continue
        qualification = qualified.get(profile.profile_id)
        if qualification is not None and qualification.version == version:
            status, reason = "QUALIFIED", "live_qualification_verified"
        elif qualification is not None:
            status, reason = "BLOCKED", "qualification_version_changed"
        else:
            status, reason = "BLOCKED", "awaiting_live_qualification"
        results.append(ProfileCompatibility(profile.profile_id, True, version, status, reason))
    return tuple(results)


def _resolve_executable(executable: str) -> Path | None:
    resolved = shutil.which(executable)
    return Path(resolved) if resolved is not None else None


def _run_version(argv: tuple[str, ...]) -> str:
    completed = subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
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
        environment,
        _READINESS_MARKERS[str(definition.profile_id)],
        definition.graceful_keys,
        _READINESS_BLOCKERS.get(str(definition.profile_id), ()),
    )
