"""Live drill: the prompt relay types into real idle agent panes, and is refused by busy ones.

Everything about `send_prompt` is pinned at the argv level in `tests/unit/adapters/tmux/`, against
captures of real screens. What that cannot show is a real agent receiving the paste: that the
bracketed two-line prompt lands as **one** message, that one `Enter` submits it, and that a pane
mid-turn is refused before anything is typed. So this drives the real `TmuxTerminal` and gateway
against real Claude and Codex panes on a scratch tmux server.

**Not opt-in**, like `test_steady_panes.py` and for its reason: it is a stage gate's command, and an
opt-in skip there would report green over a drill that never ran. It skips only for what the host
lacks -- a binary or its credentials -- decided before a pane opens. It spends a few short turns
on the owner's accounts (Claude on `sonnet`).

Boundaries: a `remote-agents-test-*` socket that is destroyed; a disposable workspace per agent;
Codex in a disposable `CODEX_HOME` whose `auth.json` is a link to the owner's, never opened here;
Claude with the owner's own HOME, because a fresh config directory demands an interactive login.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_panes import (
    CLAUDE_OPENING,
    CLAUDE_READY,
    CODEX_OPENING,
    CODEX_READY,
    Interstitial,
    open_to_composer,
)

from remote_agents.adapters.agents.registry import (
    install_agent_hooks,
    profile_composers,
    profiles_with_finished_events,
    provider_descriptors,
)
from remote_agents.adapters.agents.turn_markers import FileTurnMarkers
from remote_agents.adapters.sqlite.database import open_ui_database
from remote_agents.adapters.sqlite.queued_prompt_store import SQLiteQueuedPromptStore
from remote_agents.adapters.tmux.composer import PaneState, classify
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TerminalWaits, TmuxTerminal
from remote_agents.application.activity import drain_activity
from remote_agents.application.prompt_relay import PromptRelay
from remote_agents.composition.service import (
    _FAST_CHECK_SECONDS,
    _check_waiting_turns_once,
    _retry_waiting_messages,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity
from remote_agents.ports.message_relay import RelayOutcome, RelayResult
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE
from remote_agents.ports.terminal import PromptOutcome, PromptReason

_LINE_ONE = "Reply with exactly one word: relayed."
_LINE_TWO = "This second line belongs to the same message."
_LONG_TURN = (
    "Run the shell command `sleep 20` in the foreground, not in the background, then reply "
    "with exactly one word: slept."
)
"""A turn that is busy on screen for its whole length. A counting turn is not: while an agent
streams its answer, Claude 2.1.280 draws no busy line at all and Codex 0.155.1 marks it only in
its title (measured 2026-09-23), so a refusal checked mid-stream reads an idle composer. Both draw
their busy line for as long as a command runs (BL-108 records Claude's streaming gap) -- in the
foreground: Claude 2.1.280 ran a bare `sleep 30` as a background shell and ended its turn after
8s, with the sleep still running (measured 2026-09-23)."""
_QUEUED = "Reply with exactly one word: delivered."
_MULTILINE_STATUS = {
    "type": "command",
    "command": (
        "printf 'Sonnet | relay@main\\n\u2699 some-plan 1/2\\n\u2514\u2500 \u2699 sub-plan 3/12\\n'"
    ),
}
_LONGER_TURN = (
    "Run the shell command `sleep 30` in the foreground, not in the background, then reply "
    "with exactly one word: slept."
)
"""For the queue drill, which needs the turn still running after `send_prompt` has confirmed
it. A counting turn was used until 2026-09-23: Sonnet counted to 150 before the second message
arrived in one full run, and the relay then -- correctly -- typed it straight in. Claude 2.1.282
no longer runs it as written: its own harness blocks a long foreground `sleep` ("The harness
blocked the foreground sleep 30"), and the turn ends after about 5 s with that refusal
(measured 2026-09-24). The queue still holds, because the refusal is drawn busy, but the drills
that need a turn running longer stream a count instead and rely on the turn marker
(`_STREAMING_TURN`)."""


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments], capture_output=True, text=True, timeout=60, check=False
    )


def _requirements(agent: str) -> None:
    for needed in (agent, "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    if agent == "claude" and not (Path.home() / ".claude" / ".credentials.json").is_file():
        pytest.skip("BLOCKED: claude is not logged in")
    if agent == "codex" and not (Path.home() / ".codex" / "auth.json").is_file():
        pytest.skip("BLOCKED: codex is not logged in with ChatGPT")


def _open_pane(
    agent: str, socket: str, workspace: Path, *, spool: Path | None = None
) -> tuple[SessionId, list[str]]:
    """A real agent pane on the scratch server, marked as a managed session, at its composer.

    With `spool`, the agent's real "finished" hook is installed for this pane only -- Claude's
    through a `--settings` file, Codex's into its disposable home -- writing to that spool, and
    the pane carries the managed-session variable the hook reads.
    """
    session_id = SessionId.new()
    command = ["claude", "--model", "sonnet"] if agent == "claude" else ["codex"]
    environment: list[str] = []
    if spool is not None:
        environment = ["-e", f"{SESSION_ID_VARIABLE}={session_id}"]
    if agent == "claude":
        # A status line of three lines, as the owner's is while a plan is in flight: with the
        # mode line that is four under the composer, which read as UNKNOWN until 0.47.1 and had
        # every message refused. No drill ran with more than one until then.
        settings = workspace.parent / "claude-settings.json"
        settings.write_text(
            json.dumps({"model": "sonnet", "statusLine": _MULTILINE_STATUS}) + "\n",
            encoding="utf-8",
        )
        if spool is not None:
            install_agent_hooks(settings, executable=Path(sys.executable), activity_directory=spool)
        command += ["--settings", str(settings)]
    if agent == "codex":
        codex_home = workspace / ".codex-home"
        codex_home.mkdir(mode=0o700)
        os.symlink(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")
        # The model nudge is hidden because an account near its weekly limit raises it after
        # every turn, and the relay rightly refuses a dialog (measured 2026-09-25, Codex 0.155.1).
        (codex_home / "config.toml").write_text(
            'approval_policy = "on-request"\nsandbox_mode = "read-only"\n'
            "[notice]\nhide_rate_limit_model_nudge = true\n",
            encoding="utf-8",
        )
        if spool is not None:
            install_agent_hooks(
                codex_home / "hooks.json",
                executable=Path(sys.executable),
                activity_directory=spool,
                provider="codex",
            )
        environment += ["-e", f"CODEX_HOME={codex_home}"]
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)
    _tmux(
        socket, "new-session", "-d", "-s", f"ra-{session_id}", "-x", "160", "-y", "40",
        "-c", str(workspace), *environment, *command,
    )  # fmt: skip
    pane = _tmux(socket, "list-panes", "-t", f"=ra-{session_id}:", "-F", "#{pane_id}").stdout
    pane = pane.strip()
    for option, value in (
        ("@remote_agents_schema", "2"),
        ("@remote_agents_id", str(session_id)),
        ("@remote_agents_project_id", "qualification"),
        ("@remote_agents_profile", agent),
    ):
        _tmux(socket, "set-option", "-p", "-t", pane, option, value)
    ready, opening = (
        (CLAUDE_READY, CLAUDE_OPENING) if agent == "claude" else (CODEX_READY, CODEX_OPENING)
    )
    _answer(socket, pane, agent, ready, opening)
    return session_id, [pane]


def _answer(
    socket: str,
    pane: str,
    agent: str,
    ready: str | tuple[str, ...],
    opening: tuple[Interstitial, ...],
) -> None:
    open_to_composer(
        lambda: _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        lambda key: (_tmux(socket, "send-keys", "-t", pane, key), time.sleep(0.5)),
        ready=ready,
        interstitials=opening,
        agent=agent,
    )


def _terminal(socket: str, locks: Path, *, spool: Path | None = None) -> TmuxTerminal:
    """The runtime as the service builds it; with `spool`, it reads the turn markers the pane's
    installed hook writes there, as the service reads the ones under its activity directory."""
    return TmuxTerminal(
        TmuxGateway(socket, AsyncTmuxRunner(), key_lock_directory=locks),
        {},
        {},
        startup_timeout=1.0,
        composers=profile_composers(),
        waits=TerminalWaits(prompt_settle=1.0, prompt_bound=45.0),
        turn_markers=None if spool is None else FileTurnMarkers(spool),
    )


def _wait_for(socket: str, pane: str, needle: str, seconds: float = 120.0) -> str:
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        text = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-200", "-t", pane).stdout
        if needle in text:
            return text
        time.sleep(1.0)
    pytest.fail(f"{needle!r} never appeared in the pane:\n{text}")


def _wait_until_idle(socket: str, pane: str, agent: str, seconds: float = 120.0) -> None:
    descriptor = profile_composers()[agent]
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        # Read as the relay reads it: styled, and with the pane's title, which is the only place
        # Codex marks a turn while it streams its answer.
        title = _tmux(socket, "display-message", "-p", "-t", pane, "#{pane_title}").stdout
        text = _tmux(socket, "capture-pane", "-p", "-e", "-t", pane).stdout
        if classify(text, descriptor, title.rstrip("\n")) is PaneState.IDLE:
            return
        time.sleep(1.0)
    pytest.fail(f"{agent} never came back to an idle composer:\n{text}")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_runtime_types_into_an_idle_pane_and_is_refused_by_a_busy_one(
    agent: str, tmp_path: Path
) -> None:
    _requirements(agent)
    socket = f"remote-agents-test-relay-{SessionId.new().value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        session_id, (pane,) = _open_pane(agent, socket, workspace)
        terminal = _terminal(socket, tmp_path / "locks")

        # 1. Idle: a two-line message is delivered, and lands as one message.
        delivery = asyncio.run(terminal.send_prompt(session_id, f"{_LINE_ONE}\n{_LINE_TWO}"))
        assert delivery.outcome is PromptOutcome.SENT, (
            delivery,
            _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        )
        screen = _wait_for(socket, pane, _LINE_TWO)
        lines = [line.strip() for line in screen.splitlines()]
        first = next(index for index, line in enumerate(lines) if _LINE_ONE in line)
        assert _LINE_TWO in lines[first + 1], (
            f"the two lines did not arrive as one message:\n{screen}"
        )
        assert lines.count(next(line for line in lines if _LINE_ONE in line)) == 1, (
            f"the message was submitted more than once:\n{screen}"
        )

        # 2. Busy: a long turn is started, and a message sent meanwhile is refused untyped.
        # First the reply to (1) must finish: read by the same classifier the relay uses, not
        # by a word that the prompt itself contains.
        _wait_until_idle(socket, pane, agent)
        started = asyncio.run(terminal.send_prompt(session_id, _LONG_TURN))
        assert started.outcome is PromptOutcome.SENT, started
        refused = asyncio.run(terminal.send_prompt(session_id, "This must never be typed."))
        assert (refused.outcome, refused.reason) == (PromptOutcome.REFUSED, PromptReason.BUSY), (
            refused,
            _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        )
        after = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-400", "-t", pane).stdout
        assert "This must never be typed." not in after
    finally:
        _tmux(socket, "kill-server")


_STREAMING_TURN = "Count from 1 to 600, one number per line, and nothing else."
_SPINNER = re.compile(r"^[\u2800-\u28ff] ")


def test_a_codex_turn_streaming_its_answer_is_refused_by_its_title(tmp_path: Path) -> None:
    """While Codex streams its answer its screen reads idle; only the title says the turn runs.

    Measured on 0.155.1 (2026-09-23): no busy line is drawn while the answer streams, and the
    composer rows are the bytes of a finished turn. The drill waits for exactly that state -- the
    screen alone reads IDLE while the title spins -- and sends into it. Codex only: Claude draws
    nothing in either place while it streams (BL-108).
    """
    _requirements("codex")
    socket = f"remote-agents-test-relay-{SessionId.new().value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    descriptor = profile_composers()["codex"]
    try:
        session_id, (pane,) = _open_pane("codex", socket, workspace)
        terminal = _terminal(socket, tmp_path / "locks")
        started = asyncio.run(terminal.send_prompt(session_id, _STREAMING_TURN))
        assert started.outcome is PromptOutcome.SENT, started

        deadline = time.monotonic() + 90.0
        title = screen = ""
        while time.monotonic() < deadline:
            title = _tmux(socket, "display-message", "-p", "-t", pane, "#{pane_title}").stdout
            screen = _tmux(socket, "capture-pane", "-p", "-e", "-t", pane).stdout
            if _SPINNER.match(title) and classify(screen, descriptor) is PaneState.IDLE:
                break
            time.sleep(0.2)
        else:
            pytest.fail(f"never caught Codex streaming with an idle-looking screen:\n{screen}")

        refused = asyncio.run(terminal.send_prompt(session_id, "This must never be typed."))
        assert (refused.outcome, refused.reason) == (PromptOutcome.REFUSED, PromptReason.BUSY), (
            refused,
            title,
        )
        _wait_until_idle(socket, pane, "codex")
        after = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-1000", "-t", pane).stdout
        assert "This must never be typed." not in after
    finally:
        _tmux(socket, "kill-server")


class _OneSession:
    """The relay's one question of the store: is this session still running."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.record if session_id == self.record.session_id else None


def _wait_for_finished(spool: Path, session_id: SessionId, seconds: float = 240.0):
    """Drain the spool the way the service pass does, until this session's turn has finished."""
    deadline = time.monotonic() + seconds
    drained: list[AgentActivity] = []
    while time.monotonic() < deadline:
        drained.extend(drain_activity(spool))
        if any(
            activity.session_id == str(session_id) and activity.kind is ActivityKind.COMPLETED
            for activity in drained
        ):
            return drained
        time.sleep(1.0)
    pytest.fail(f"no finished event was spooled for the turn: {drained}")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_queue_behind_a_real_turn_and_deliver_after_its_stop(agent: str, tmp_path: Path) -> None:
    """Busy pane: the message queues; the real Stop hook's record delivers it, exactly once."""
    _requirements(agent)
    socket = f"remote-agents-test-relay-{SessionId.new().value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spool = tmp_path / "activity"
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    try:
        session_id, (pane,) = _open_pane(agent, socket, workspace, spool=spool)
        terminal = _terminal(socket, tmp_path / "locks")
        record = SessionRecord(
            session_id,
            ProjectId("q" * 24),
            ProfileId(agent),
            SessionDisplayIdentity("Relay", agent, "regular", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
        )
        finishing = profiles_with_finished_events(provider_descriptors())
        relay = PromptRelay(
            terminal,
            SQLiteQueuedPromptStore(connection),
            _OneSession(record),
            queues_for=lambda profile: str(profile) in finishing,
        )
        drain_activity(spool)

        # 1. A real turn is running, and a message sent meanwhile is queued, untyped.
        started = asyncio.run(terminal.send_prompt(session_id, _LONGER_TURN))
        assert started.outcome is PromptOutcome.SENT, started
        queued = asyncio.run(relay.submit(session_id, _QUEUED))
        assert queued.outcome is RelayOutcome.QUEUED, (
            queued,
            _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        )

        # 2. The turn ends; the service pass drains its real Stop record and retries.
        activities = _wait_for_finished(spool, session_id)
        announced: list[tuple[str, RelayResult]] = []

        async def announce(session: str, result: RelayResult) -> None:
            announced.append((session, result))

        asyncio.run(
            _retry_waiting_messages(
                SimpleNamespace(prompt_relay=relay, relay_announcer=announce), activities
            )
        )

        # 3. It was typed, it was submitted, and it appears exactly once.
        assert [(session, result.outcome) for session, result in announced] == [
            (str(session_id), RelayOutcome.SENT)
        ], (announced, _tmux(socket, "capture-pane", "-p", "-t", pane).stdout)
        assert relay.pending(session_id) is None
        screen = _wait_for(socket, pane, _QUEUED)
        assert sum(_QUEUED in line for line in screen.splitlines()) == 1, (
            f"the queued message was typed more than once:\n{screen}"
        )
    finally:
        connection.close()
        _tmux(socket, "kill-server")


_COUNTED = re.compile(r"^\W*\d+\s*$")
_INTERRUPTED = {"claude": "Interrupted", "codex": "Conversation interrupted"}
"""What each agent draws above its box once Esc has stopped a turn: Claude's from the real-pane
captures under `tests/fixtures/panes/turn_states/`, Codex's measured on 0.155.1 (2026-09-25),
where Esc fires no hook and stops the title spinner."""


def _relay(agent: str, session_id: SessionId, terminal: TmuxTerminal, connection) -> PromptRelay:
    record = SessionRecord(
        session_id,
        ProjectId("q" * 24),
        ProfileId(agent),
        SessionDisplayIdentity("Relay", agent, "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    finishing = profiles_with_finished_events(provider_descriptors())
    return PromptRelay(
        terminal,
        SQLiteQueuedPromptStore(connection),
        _OneSession(record),
        queues_for=lambda profile: str(profile) in finishing,
    )


def _wait_until_streaming(
    socket: str, pane: str, agent: str, markers: FileTurnMarkers, session_id: SessionId
) -> None:
    """Until the answer is streaming, its turn is marked, and the screen alone reads idle.

    That last reading is the gap this drill exists for (BL-108): without the marker the relay
    would type into this turn. Waiting for it, rather than for a fixed time, is what makes the
    queueing below a proof rather than a race won by the turn's start.
    """
    descriptor = profile_composers()[agent]
    deadline = time.monotonic() + 90.0
    plain = ""
    reading = PaneState.UNKNOWN
    while time.monotonic() < deadline:
        plain = _tmux(socket, "capture-pane", "-p", "-t", pane).stdout
        styled = _tmux(socket, "capture-pane", "-p", "-e", "-t", pane).stdout
        counted = sum(bool(_COUNTED.match(line)) for line in plain.splitlines())
        # The screen alone, without the pane title, deliberately: that reading is the gap. With
        # the title, Codex's spinner would read BUSY here whatever the marker said.
        reading = classify(styled, descriptor)
        if (
            counted >= 10
            and markers.started_at(str(session_id)) is not None
            and reading is PaneState.IDLE
        ):
            return
        time.sleep(0.2)
    pytest.fail(
        f"never caught {agent} streaming a marked turn, screen reading idle; it last read "
        f"{reading.name} (DIALOG: look for a rate-limit or usage prompt before blaming the "
        f"relay):\n{plain}"
    )


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_a_streaming_answer_queues_the_message_until_its_stop(agent: str, tmp_path: Path) -> None:
    """Mid-stream, the screen reads idle and only the marker says the turn runs (DEC-104).

    On Codex the title spinner says so too, so there the marker is a second witness rather than
    the only one. The message queues; the fast check, ticking through the rest of the turn, never
    types it; the real Stop record then delivers it, exactly once.
    """
    _requirements(agent)
    socket = f"remote-agents-test-relay-{SessionId.new().value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spool = tmp_path / "activity"
    markers = FileTurnMarkers(spool)
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    announced: list[tuple[str, RelayResult]] = []

    async def announce(session: str, result: RelayResult) -> None:
        announced.append((session, result))

    try:
        session_id, (pane,) = _open_pane(agent, socket, workspace, spool=spool)
        terminal = _terminal(socket, tmp_path / "locks", spool=spool)
        relay = _relay(agent, session_id, terminal, connection)
        composition = SimpleNamespace(
            prompt_relay=relay, relay_announcer=announce, turn_markers=markers, relay_rechecks={}
        )
        drain_activity(spool)

        # 1. A marked turn streams with an idle-looking screen; a message sent now is queued.
        started = asyncio.run(terminal.send_prompt(session_id, _STREAMING_TURN))
        assert started.outcome is PromptOutcome.SENT, started
        _wait_until_streaming(socket, pane, agent, markers, session_id)
        queued = asyncio.run(relay.submit(session_id, _QUEUED))
        assert queued.outcome is RelayOutcome.QUEUED, (
            queued,
            _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        )

        # 2. The fast check ticks through the rest of the turn and types nothing.
        drained: list[AgentActivity] = []
        deadline = time.monotonic() + 240.0
        while not any(
            activity.session_id == str(session_id) and activity.kind is ActivityKind.COMPLETED
            for activity in drained
        ):
            if time.monotonic() > deadline:
                screen = _tmux(socket, "capture-pane", "-p", "-t", pane).stdout
                pytest.fail(f"no finished event was spooled in 240 s: {drained}\n{screen}")
            asyncio.run(_check_waiting_turns_once(composition))
            assert relay.pending(session_id) is not None, (
                "the fast check typed into a running turn",
                announced,
                _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
            )
            time.sleep(0.5)
            drained.extend(drain_activity(spool))
        before = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-2000", "-t", pane).stdout
        assert _QUEUED not in before, f"the message was typed before the turn's Stop:\n{before}"

        # 3. The Stop record retries it, and the fast check covers the Stop-hooks spinner.
        asyncio.run(_retry_waiting_messages(composition, drained))
        deadline = time.monotonic() + 20.0
        while relay.pending(session_id) is not None and time.monotonic() < deadline:
            time.sleep(0.5)
            asyncio.run(_check_waiting_turns_once(composition))
        delivered = [result.outcome for _, result in announced]
        assert [outcome for outcome in delivered if outcome is not RelayOutcome.QUEUED] == [
            RelayOutcome.SENT
        ], (announced, _tmux(socket, "capture-pane", "-p", "-t", pane).stdout)
        assert relay.pending(session_id) is None
        screen = _wait_for(socket, pane, _QUEUED)
        assert sum(_QUEUED in line for line in screen.splitlines()) == 1, (
            f"the queued message was typed more than once:\n{screen}"
        )
    finally:
        connection.close()
        _tmux(socket, "kill-server")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_an_interrupted_turn_releases_its_queued_message(agent: str, tmp_path: Path) -> None:
    """Esc fires no hook; the fast check reads the end line and delivers the queued message.

    Ticked at the service's own `_FAST_CHECK_SECONDS`, so the bound is what the owner sees: at
    most one tick plus the redraw after Esc, which is the "about 3 s" the design promises. The
    assertion allows 6 s -- two ticks and slack -- so a slow redraw is not read as a defect.
    """
    _requirements(agent)
    socket = f"remote-agents-test-relay-{SessionId.new().value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spool = tmp_path / "activity"
    markers = FileTurnMarkers(spool)
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    announced: list[tuple[str, RelayResult]] = []

    async def announce(session: str, result: RelayResult) -> None:
        announced.append((session, result))

    try:
        session_id, (pane,) = _open_pane(agent, socket, workspace, spool=spool)
        terminal = _terminal(socket, tmp_path / "locks", spool=spool)
        relay = _relay(agent, session_id, terminal, connection)
        composition = SimpleNamespace(
            prompt_relay=relay, relay_announcer=announce, turn_markers=markers, relay_rechecks={}
        )

        started = asyncio.run(terminal.send_prompt(session_id, _STREAMING_TURN))
        assert started.outcome is PromptOutcome.SENT, started
        _wait_until_streaming(socket, pane, agent, markers, session_id)
        queued = asyncio.run(relay.submit(session_id, _QUEUED))
        assert queued.outcome is RelayOutcome.QUEUED, queued

        # Timed from the key, not from the line: Codex stops its spinner as Esc lands, and the
        # fast check can deliver before a capture here has caught the line being drawn.
        _tmux(socket, "send-keys", "-t", pane, "Escape")
        escaped_at = time.monotonic()
        delivered_at: float | None = None
        while time.monotonic() < escaped_at + 30.0:
            time.sleep(_FAST_CHECK_SECONDS)
            asyncio.run(_check_waiting_turns_once(composition))
            if relay.pending(session_id) is None:
                delivered_at = time.monotonic()
                break
        screen = _tmux(socket, "capture-pane", "-p", "-t", pane).stdout

        assert delivered_at is not None and delivered_at - escaped_at <= 6.0, (
            None if delivered_at is None else delivered_at - escaped_at,
            announced,
            screen,
        )
        assert [(session, result.outcome) for session, result in announced] == [
            (str(session_id), RelayOutcome.SENT)
        ], (announced, screen)
        # Delivered because the turn ended: its interrupt line stands above the message.
        screen = _wait_for(socket, pane, _QUEUED)
        lines = screen.splitlines()
        interrupted = [index for index, line in enumerate(lines) if _INTERRUPTED[agent] in line]
        delivered = [index for index, line in enumerate(lines) if _QUEUED in line]
        assert interrupted and delivered and interrupted[-1] < delivered[0], (
            f"the message did not follow an interrupt line:\n{screen}"
        )
        assert sum(_QUEUED in line for line in screen.splitlines()) == 1, (
            f"the queued message was typed more than once:\n{screen}"
        )
    finally:
        connection.close()
        _tmux(socket, "kill-server")
