"""Disposable verification of the tmux features required by the isolated adapter."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.codec import exact_session_target


@dataclass(frozen=True, slots=True)
class FeatureProbeResult:
    """Evidence that a generated test socket supports the required tmux contract."""

    socket_name: str
    exact_target: str
    user_option: str
    capture_is_text: bool


def probe_features(working_directory: Path) -> FeatureProbeResult:
    """Exercise named-socket, exact-target, options, and capture support without a shell."""
    socket_name = f"remote-agents-test-{uuid4().hex}"
    session_name = f"ra-{uuid4()}"
    target = exact_session_target(session_name)
    base = ("tmux", "-L", socket_name)
    try:
        _run(*base, "new-session", "-d", "-s", session_name, "-c", str(working_directory))
        _run(*base, "set-option", "-t", target, "remain-on-exit", "on")
        _run(*base, "set-option", "-t", target, "@remote_agents_schema", "1")
        user_option = _run(*base, "display-message", "-p", "-t", target, "#{@remote_agents_schema}")
        captured = _run(*base, "capture-pane", "-p", "-t", target, "-S", "-5")
        return FeatureProbeResult(
            socket_name, target, user_option.strip(), isinstance(captured, str)
        )
    finally:
        _run(*base, "kill-session", "-t", target, check=False)
        _stale_socket_path(socket_name).unlink(missing_ok=True)


def _run(*arguments: str, check: bool = True) -> str:
    """Run a fixed tmux argv and decode its text-only contract output."""
    result = subprocess.run(arguments, check=check, text=True, capture_output=True)
    return result.stdout


def _stale_socket_path(socket_name: str) -> Path:
    """Locate only the generated disposable server socket after its session exits."""
    return Path(f"/tmp/tmux-{os.getuid()}") / socket_name
