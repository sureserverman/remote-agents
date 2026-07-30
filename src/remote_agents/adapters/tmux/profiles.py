"""Local executable/version checks for closed profile definitions."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from remote_agents.domain.profiles import ProfileCompatibility, ProfileDefinition


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
            version = _sanitize_version(run_version(profile.version_command))
        except (OSError, subprocess.SubprocessError):
            results.append(
                ProfileCompatibility(
                    profile.profile_id, True, None, "BLOCKED", "version_probe_failed"
                )
            )
            continue
        results.append(
            ProfileCompatibility(
                profile.profile_id,
                True,
                version,
                "BLOCKED",
                "awaiting_live_qualification",
            )
        )
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
