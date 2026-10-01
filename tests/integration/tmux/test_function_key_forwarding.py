"""The F-key root layer, delivered by a real tmux server to the pane that should have it.

Everything else about this layer is asserted at argv level, which proves the *string* is right
and nothing about what tmux does with it. This drives the script: a disposable server, real
panes carrying the real marks, a real client pressing the key, and an assertion about which
pane's stdin the key arrived on.

**The key is pressed by an attached client, not injected with `send-keys`, and the difference
is the whole test.** `send-keys` writes into a pane's pty and never consults a key table, so a
root binding it is meant to exercise would not fire at all — the test would pass by delivering
the key itself. A client on a pty is the only mechanism that reaches `bind-key -n`.

Each pane runs `cat` into a file of its own, so "which pane received the key" is a question
about bytes on disk rather than about a screen scrape.
"""

from __future__ import annotations

import os
import pty
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME, console_binding_args
from remote_agents.ports.console import ConsoleBindingAction, ConsolePaneSlot

#: What a terminal sends for the keys under test. tmux resolves `F2`/`F5`; a client types bytes.
_SEQUENCES = {"F2": "\x1bOQ", "F5": "\x1b[15~"}

#: The reservation the registry holds today, passed in rather than imported: this file is about
#: delivery, and `tests/provider_contract` owns the measurement.
_RESERVED = {"claude": frozenset(), "opencode": frozenset({"F2"})}

_SLOT_OPTION = "@remote_agents_console_slot"
_PROFILE_OPTION = "@remote_agents_profile"


class _Server:
    """A disposable tmux server with a console session and panes we can mark and watch."""

    def __init__(self, tmp_path: Path) -> None:
        self.socket = f"remote-agents-fkey-{uuid4().hex}"
        self.tmp = tmp_path
        self._clients: list[int] = []

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args], capture_output=True, text=True, check=check
        )

    def start(self) -> None:
        # The session name is load-bearing: the script's first clause refuses any client whose
        # session is not the console's, which is DEC-073(3)'s fence.
        self.tmux("new-session", "-d", "-s", CONSOLE_SESSION_NAME, "-x", "80", "-y", "24", "cat")
        time.sleep(0.3)

    def add_pane(self) -> str:
        self.tmux("split-window", "-t", f"{CONSOLE_SESSION_NAME}:", "cat")
        time.sleep(0.3)
        return self.panes()[-1]

    def panes(self) -> list[str]:
        listed = self.tmux(
            "list-panes", "-t", f"{CONSOLE_SESSION_NAME}:", "-F", "#{pane_id}"
        ).stdout
        return listed.split()

    def watch(self, pane: str) -> Path:
        """Point this pane at a file, in raw mode, so what it receives is readable afterwards.

        **`stty raw -echo` is not tidiness, it is the difference between a working test and one
        that measures nothing.** A pane's pty starts in canonical mode, where the line
        discipline holds input until a newline — and a function key is an escape sequence with
        no newline in it, so a plain `cat` receives the key and writes nothing, for ever. The
        first version of this file did exactly that: every positive case failed while the two
        negative cases passed, which is the shape to be suspicious of, since "nothing was
        delivered" is what they assert.
        """
        sink = self.tmp / f"{pane.lstrip('%')}.out"
        self.tmux("respawn-pane", "-k", "-t", pane, f"sh -c 'stty raw -echo; cat > {sink}'")
        time.sleep(0.3)
        return sink

    def mark(self, pane: str, *, slot: str | None = None, profile: str | None = None) -> None:
        if slot is not None:
            self.tmux("set-option", "-p", "-t", pane, _SLOT_OPTION, slot)
        if profile is not None:
            self.tmux("set-option", "-p", "-t", pane, _PROFILE_OPTION, profile)

    def install(self, key: str) -> None:
        self.tmux(
            *console_binding_args(
                key, ConsoleBindingAction.FORWARD_FUNCTION_KEY, reserved_keys=_RESERVED
            )
        )

    def attach(self, session: str = CONSOLE_SESSION_NAME) -> int:
        """A real client on a real pty — the only thing that makes a root binding fire."""
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child execs away
            os.execvp("tmux", ["tmux", "-L", self.socket, "attach-session", "-t", session])
        self._clients.append(fd)
        time.sleep(1.0)
        return fd

    def press(self, fd: int, key: str) -> None:
        os.write(fd, _SEQUENCES[key].encode())
        time.sleep(1.0)

    def stop(self) -> None:
        for fd in self._clients:
            try:
                os.close(fd)
            except OSError:
                pass
        self.tmux("kill-server", check=False)


def _received(sink: Path, key: str, *, timeout: float = 5.0) -> bool:
    """Whether this pane was handed the key's own escape sequence, polled to a deadline.

    Polled rather than read once for the reason the rest of this suite polls: `send-keys`
    returns when tmux has queued the key, not when the pane's process has written it, and the
    gap is scheduling. A single read makes the test a race this machine happens to win.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sink.exists() and _SEQUENCES[key] in sink.read_text(errors="replace"):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server(tmp_path: Path):
    running = _Server(tmp_path)
    try:
        running.start()
        yield running
    finally:
        running.stop()


def test_a_key_pressed_in_one_of_our_own_panes_goes_back_to_that_pane(server: _Server) -> None:
    """Branch 1: the owner is in a console pane, so the surface running there owns the key."""
    sessions, ours = server.panes()[0], server.add_pane()
    sessions_sink, ours_sink = server.watch(sessions), server.watch(ours)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    server.mark(ours, slot=ConsolePaneSlot.FEED.value)
    server.install("F5")
    server.tmux("select-pane", "-t", ours)

    server.press(server.attach(), "F5")

    assert _received(ours_sink, "F5"), "the console's own pane did not receive the key"
    assert not _received(sessions_sink, "F5"), "the key was also delivered to the sessions pane"


def test_a_reserved_key_pressed_in_that_agent_s_pane_is_handed_over(server: _Server) -> None:
    """Branch 2, and the reason this layer has a branch 2 at all.

    OpenCode binds F2. Without the pass-through the console would take it from an owner who
    never asked the console for anything — and the only symptom would be one key doing the
    wrong thing inside one agent.
    """
    sessions, agent = server.panes()[0], server.add_pane()
    sessions_sink, agent_sink = server.watch(sessions), server.watch(agent)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    server.mark(agent, profile="opencode")
    server.install("F2")
    server.tmux("select-pane", "-t", agent)

    server.press(server.attach(), "F2")

    assert _received(agent_sink, "F2"), "OpenCode's own key was taken from its pane"
    assert not _received(sessions_sink, "F2"), "a reserved key also reached the sessions pane"


def test_a_key_that_agent_does_not_reserve_goes_to_the_sessions_pane(server: _Server) -> None:
    """Branch 3 from the same pane as branch 2, which is what makes the reservation per key."""
    sessions, agent = server.panes()[0], server.add_pane()
    sessions_sink, agent_sink = server.watch(sessions), server.watch(agent)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    server.mark(agent, profile="opencode")
    server.install("F5")
    server.tmux("select-pane", "-t", agent)

    server.press(server.attach(), "F5")

    assert _received(sessions_sink, "F5"), "the key never reached the sessions pane"
    assert not _received(agent_sink, "F5"), "a key OpenCode does not reserve stayed in its pane"


def test_a_reserved_key_pressed_in_another_agent_s_pane_still_goes_to_the_sessions_pane(
    server: _Server,
) -> None:
    """The reservation belongs to the provider, not to the key: Claude does not bind F2."""
    sessions, agent = server.panes()[0], server.add_pane()
    sessions_sink, agent_sink = server.watch(sessions), server.watch(agent)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    server.mark(agent, profile="claude")
    server.install("F2")
    server.tmux("select-pane", "-t", agent)

    server.press(server.attach(), "F2")

    assert _received(sessions_sink, "F2"), "the key never reached the sessions pane"
    assert not _received(agent_sink, "F2"), "F2 stayed in a pane whose agent does not bind it"


def test_a_client_attached_to_another_session_delivers_nothing(server: _Server) -> None:
    """DEC-073(3): a key table belongs to the *server*, and managed agents attach to it.

    Without the guard the key fires from any client on the socket — including a plain
    `remote-agents attach ra-<uuid>`, a terminal with no console pane on screen at all.
    """
    sessions = server.panes()[0]
    sessions_sink = server.watch(sessions)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    server.install("F5")
    server.tmux("new-session", "-d", "-s", "ra-somewhere-else", "cat")

    server.press(server.attach("ra-somewhere-else"), "F5")

    assert not _received(sessions_sink, "F5"), (
        "a client attached to another session on this socket reached the console's panes"
    )


def test_two_panes_carrying_the_sessions_mark_deliver_nothing(server: _Server) -> None:
    """BL-042's stricter arm: ambiguity delivers nothing rather than picking a winner.

    The retired prefix layer took `head -n 1`. The key this forwards can be a stop issued
    without asking (DEC-018), so an arbitrary winner means stopping a session in whichever
    console won — which is not a failure mode a root key may have.
    """
    first, second = server.panes()[0], server.add_pane()
    agent = server.add_pane()
    first_sink, second_sink = server.watch(first), server.watch(second)
    server.watch(agent)
    server.mark(first, slot=ConsolePaneSlot.SESSIONS.value)
    server.mark(second, slot=ConsolePaneSlot.SESSIONS.value)
    server.mark(agent, profile="claude")
    server.install("F5")
    server.tmux("select-pane", "-t", agent)

    server.press(server.attach(), "F5")

    assert not _received(first_sink, "F5") and not _received(second_sink, "F5"), (
        "an ambiguous sessions mark still delivered the key to one of the candidates"
    )


# --- F2 is Settings, and Settings opens in the projects pane (2026-10-01) -----------------


def _install_settings(server: _Server, projects_command: tuple[str, ...] = ("true",)) -> None:
    server.tmux(
        *console_binding_args(
            "F2", ConsoleBindingAction.SHOW_SETTINGS, projects_command, reserved_keys=_RESERVED
        )
    )


def _active(server: _Server) -> str:
    return server.tmux(
        "display-message", "-p", "-t", f"{CONSOLE_SESSION_NAME}:", "#{pane_id}"
    ).stdout.strip()


def test_settings_pressed_in_a_right_hand_pane_opens_in_the_projects_pane(
    server: _Server,
) -> None:
    """The owner's ask: Settings on the left, whichever of the console's panes is active."""
    projects, sessions = server.panes()[0], server.add_pane()
    projects_sink, sessions_sink = server.watch(projects), server.watch(sessions)
    server.mark(projects, slot=ConsolePaneSlot.PROJECTS.value)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    _install_settings(server)
    server.tmux("select-pane", "-t", sessions)

    server.press(server.attach(), "F2")

    assert _received(projects_sink, "F2"), "Settings did not reach the projects pane"
    assert not _received(sessions_sink, "F2"), "F2 also opened Settings in the sessions pane"
    assert _active(server) == projects, "the projects pane was not selected for the owner"


def test_settings_at_an_opencode_pane_is_still_handed_to_opencode(server: _Server) -> None:
    """OpenCode binds F2 (DEC-070); moving Settings must not take its model switch away."""
    projects, agent = server.panes()[0], server.add_pane()
    projects_sink, agent_sink = server.watch(projects), server.watch(agent)
    server.mark(projects, slot=ConsolePaneSlot.PROJECTS.value)
    server.mark(agent, profile="opencode")
    _install_settings(server)
    server.tmux("select-pane", "-t", agent)

    server.press(server.attach(), "F2")

    assert _received(agent_sink, "F2"), "OpenCode's own key was taken from its pane"
    assert not _received(projects_sink, "F2"), "a reserved key also opened Settings"


def test_settings_with_the_projects_pane_parked_runs_the_projects_command_first(
    server: _Server,
) -> None:
    """An agent holds the left slot, so the surface is in another window and is fetched home.

    The command here only leaves a marker: the exchange itself is `show_projects`, tested
    where it lives. What this pins is that the script runs it, and still delivers the key.
    """
    agent = server.panes()[0]
    server.watch(agent)
    server.mark(agent, profile="claude")
    server.tmux("new-window", "-d", "-t", f"{CONSOLE_SESSION_NAME}:", "cat")
    time.sleep(0.3)
    parked = server.tmux(
        "list-panes", "-t", f"{CONSOLE_SESSION_NAME}:1", "-F", "#{pane_id}"
    ).stdout.split()[0]
    parked_sink = server.watch(parked)
    server.mark(parked, slot=ConsolePaneSlot.PROJECTS.value)
    marker = server.tmp / "fetched"
    _install_settings(server, ("touch", str(marker)))

    server.press(server.attach(), "F2")

    assert marker.exists(), "the projects command never ran for a parked projects pane"
    assert _received(parked_sink, "F2"), "Settings did not reach the projects pane"


def test_settings_with_the_projects_pane_home_does_not_run_the_projects_command(
    server: _Server,
) -> None:
    """The fast path: no Python start-up on every F2 when nothing needs exchanging."""
    projects, sessions = server.panes()[0], server.add_pane()
    projects_sink = server.watch(projects)
    server.watch(sessions)
    server.mark(projects, slot=ConsolePaneSlot.PROJECTS.value)
    server.mark(sessions, slot=ConsolePaneSlot.SESSIONS.value)
    marker = server.tmp / "fetched"
    _install_settings(server, ("touch", str(marker)))
    server.tmux("select-pane", "-t", sessions)

    server.press(server.attach(), "F2")

    assert _received(projects_sink, "F2")
    assert not marker.exists(), "the projects command ran with the surface already home"
