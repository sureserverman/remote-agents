"""Disposable verification of the tmux features required by the isolated adapter."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.codec import (
    CONSOLE_SLOT_OPTION,
    exact_session_target,
)


@dataclass(frozen=True, slots=True)
class FeatureProbeResult:
    """Evidence that a generated test socket supports the required tmux contract."""

    socket_name: str
    exact_target: str
    user_option: str
    capture_is_text: bool
    panes_splittable: bool


def probe_features(working_directory: Path) -> FeatureProbeResult:
    """Exercise named-socket, exact-target, options, capture, and pane-splitting support."""
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
            _panes_splittable(base, working_directory),
        )
    finally:
        _run(*base, "kill-session", "-t", target, check=False)
        _run(*base, "kill-session", "-t", "probe-console:", check=False)
        _stale_socket_path(socket_name).unlink(missing_ok=True)


def _panes_splittable(base: tuple[str, ...], working_directory: Path) -> bool:
    """Round-trip the console model's pane contract on the disposable socket.

    **This replaced a window-link probe**, and the replacement is the point rather than a
    rename. The console used to show a session by linking its window in as a tab, so what a
    host had to support was `link-window` plus a window-scoped option that survived the link.
    Under the swap model (DEC-040) the console is one window of three panes and it shows a
    session by exchanging panes, so a host that could link windows and not split panes would
    have passed a probe for a capability nothing uses and failed at the first `ensure`.

    Three behaviors, probed as one round trip because each is only useful with the others: a
    pane splits with an explicit percentage (`-p` was removed in tmux 3.4, so `-l N%` is the
    only form that sizes anything), tmux names the pane it created (`-P -F`), and a
    pane-scoped user option set on it reads back — which is how every console pane is found
    by what it is rather than by where it sits. A False here is what `doctor` renders when
    this host's tmux cannot host the console.
    """
    try:
        _run(*base, "new-session", "-d", "-s", "probe-console", "-c", str(working_directory))
        pane_id = _run(
            *base,
            "split-window",
            "-h",
            "-d",
            "-t",
            "probe-console:",
            "-l",
            "40%",
            "-c",
            str(working_directory),
            "-P",
            "-F",
            "#{pane_id}",
        ).strip()
        if not pane_id.startswith("%"):
            return False
        _run(*base, "set-option", "-p", "-t", pane_id, CONSOLE_SLOT_OPTION, "probe")
        read_back = _run(
            *base, "display-message", "-p", "-t", pane_id, f"#{{{CONSOLE_SLOT_OPTION}}}"
        )
        return read_back.strip() == "probe"
    except subprocess.CalledProcessError:
        return False


def _run(*arguments: str, check: bool = True) -> str:
    """Run a fixed tmux argv and decode its text-only contract output."""
    result = subprocess.run(arguments, check=check, text=True, capture_output=True)
    return result.stdout


def _stale_socket_path(socket_name: str) -> Path:
    """Locate only the generated disposable server socket after its session exits."""
    return Path(f"/tmp/tmux-{os.getuid()}") / socket_name
