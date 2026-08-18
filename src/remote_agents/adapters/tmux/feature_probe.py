"""Disposable verification of the tmux features required by the isolated adapter."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.codec import (
    CONSOLE_WINDOW_FORMAT,
    WINDOW_SESSION_OPTION,
    exact_session_target,
)


@dataclass(frozen=True, slots=True)
class FeatureProbeResult:
    """Evidence that a generated test socket supports the required tmux contract."""

    socket_name: str
    exact_target: str
    user_option: str
    capture_is_text: bool
    window_linkable: bool


def probe_features(working_directory: Path) -> FeatureProbeResult:
    """Exercise named-socket, exact-target, options, capture, and window-link support."""
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
            socket_name,
            target,
            user_option.strip(),
            isinstance(captured, str),
            _window_linkable(base, target, working_directory),
        )
    finally:
        _run(*base, "kill-session", "-t", target, check=False)
        _run(*base, "kill-session", "-t", "probe-console:", check=False)
        _stale_socket_path(socket_name).unlink(missing_ok=True)


def _window_linkable(base: tuple[str, ...], target: str, working_directory: Path) -> bool:
    """Round-trip the console model's window contract on the disposable socket.

    The console (Stage 1 of the 2026-08-18 console-surface plan) needs three behaviors at
    once: a window-scoped user option that travels with a linked window, `link-window` into
    another session, and `unlink-window` back out with the mapping still decodable. Probed
    as one round trip because each is only useful with the others; a False here is what
    `doctor` renders when this host's tmux cannot host the console.
    """
    try:
        _run(*base, "new-session", "-d", "-s", "probe-console", "-c", str(working_directory))
        _run(*base, "set-option", "-w", "-t", target, WINDOW_SESSION_OPTION, "probe")
        _run(*base, "link-window", "-s", target, "-t", "probe-console:")
        mapping = _run(
            *base,
            "list-windows",
            "-t",
            "probe-console:",
            "-F",
            CONSOLE_WINDOW_FORMAT,
        )
        linked = [
            line.split("|", 1)[0]
            for line in mapping.splitlines()
            if line.endswith("|probe")
        ]
        if len(linked) != 1:
            return False
        _run(*base, "unlink-window", "-t", f"probe-console:{linked[0]}")
        return True
    except subprocess.CalledProcessError:
        return False


def _run(*arguments: str, check: bool = True) -> str:
    """Run a fixed tmux argv and decode its text-only contract output."""
    result = subprocess.run(arguments, check=check, text=True, capture_output=True)
    return result.stdout


def _stale_socket_path(socket_name: str) -> Path:
    """Locate only the generated disposable server socket after its session exits."""
    return Path(f"/tmp/tmux-{os.getuid()}") / socket_name
