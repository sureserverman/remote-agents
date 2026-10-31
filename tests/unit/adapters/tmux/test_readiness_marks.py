"""A session is ready on the screen's word or the agent's own, and never fails for lack of one.

The 2026-10-30 incident: 172 allow rules in a project's settings made Claude print enough warnings
to hold its banner off an 80x24 pane, so a session that worked for three hours was recorded failed
at launch + 20 s and never promoted (DEC-135). The trust check still comes first in every case: a
hook or a mark can never promote a pane showing the question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_trust_dialogs
from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileStartedMarks
from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.terminal import TERMINAL_NOT_LIVE

_CLAUDE = ProfileId("claude")
_PROJECT = ProjectId("opaque-editor")
#: Claude 2.1.314 at 80x24 under a project's permission warnings: no banner anywhere on it.
_NO_BANNER = (
    "⚠ Allow rule Bash(rm -rf /tmp/ra-probe-29*.deb /tmp/ra-probe-29-stock) allows\n"
    "  any option at its * · use an exact value\n"
    "❯ \n"
    "  Opus 5.5 | ra-project | 0/1m (0%)\n"
)
_BANNER = " ▐▛███▛█   Claude Code v2.1.314\n❯ \n"
_TRUST = " Accessing workspace:\n /var/tmp/x\n ❯ No, exit\n   Yes, I trust this folder\n"


class Gateway:
    def __init__(self, session_id: SessionId, screen: str, *, alive: bool, directory: Path) -> None:
        self._session_id = session_id
        self._screen = screen
        self._alive = alive
        self.intent_directory = directory

    async def inventory(self) -> TmuxInventory:
        pane = ManagedPane(
            f"ra-{self._session_id}",
            "%1",
            True,
            self._session_id,
            _PROJECT,
            _CLAUDE,
            100,
            self._alive,
            not self._alive,
        )
        return TmuxInventory((pane,), ())

    async def capture(self, _session_id: SessionId) -> str:
        return self._screen

    async def launch(self, *_args: object) -> None:
        return None


def _profile() -> LaunchProfile:
    return LaunchProfile(
        "/usr/bin/claude",
        ("/usr/bin/claude",),
        {},
        "Claude Code",
        ("C-c",),
        ("Accessing workspace:",),
        0.0,
        None,
    )


def _terminal(
    tmp_path: Path, session_id: SessionId, screen: str, *, alive: bool = True, wired: bool = True
) -> TmuxTerminal:
    project = tmp_path / "opaque-editor"
    project.mkdir(exist_ok=True)
    return TmuxTerminal(
        Gateway(session_id, screen, alive=alive, directory=tmp_path),
        {_PROJECT: project},
        {_CLAUDE: _profile()},
        startup_timeout=0.1,
        trust_dialogs=profile_trust_dialogs(),
        started_marks=FileStartedMarks(tmp_path / "activity") if wired else None,
        conversation_marks=FileConversationMarks(tmp_path / "activity") if wired else None,
    )


def _give(tmp_path: Path, session_id: SessionId, mark: str | None) -> None:
    if mark == "started":
        FileStartedMarks(tmp_path / "activity").mark(str(session_id))
    elif mark == "conversation":
        FileConversationMarks(tmp_path / "activity").record(
            str(session_id), "5a12588a-940e-4ddc-ac7f-dccaa4fb639c"
        )


async def _settle(terminal: TmuxTerminal, session_id: SessionId):
    return await terminal._settle_readiness(session_id, _CLAUDE, _profile())


@pytest.mark.parametrize("mark", ["started", "conversation"])
async def test_no_banner_and_a_mark_is_ready(mark: str, tmp_path: Path) -> None:
    session_id = SessionId.new()
    terminal = _terminal(tmp_path, session_id, _NO_BANNER)
    _give(tmp_path, session_id, mark)

    assert (await _settle(terminal, session_id)).live
    observation = await terminal.confirm_ready(session_id, _CLAUDE)
    assert observation.live
    assert not observation.awaiting_trust


async def test_no_banner_and_no_mark_answers_as_it_did(tmp_path: Path) -> None:
    session_id = SessionId.new()
    terminal = _terminal(tmp_path, session_id, _NO_BANNER)

    settled = await _settle(terminal, session_id)
    assert not settled.live
    assert settled.detail == "startup_timeout"
    rechecked = await terminal.confirm_ready(session_id, _CLAUDE)
    assert not rechecked.live
    assert rechecked.detail == "not_ready"


async def test_another_sessions_mark_says_nothing_about_this_one(tmp_path: Path) -> None:
    session_id = SessionId.new()
    terminal = _terminal(tmp_path, session_id, _NO_BANNER)
    _give(tmp_path, SessionId.new(), "started")

    assert not (await _settle(terminal, session_id)).live


async def test_a_terminal_with_no_marks_reads_the_screen_alone(tmp_path: Path) -> None:
    session_id = SessionId.new()
    _give(tmp_path, session_id, "started")

    silent = _terminal(tmp_path, session_id, _NO_BANNER, wired=False)
    assert not (await _settle(silent, session_id)).live

    banner = _terminal(tmp_path, session_id, _BANNER, wired=False)
    assert (await _settle(banner, session_id)).live


@pytest.mark.parametrize("mark", ["started", "conversation"])
async def test_the_trust_question_beats_a_mark(mark: str, tmp_path: Path) -> None:
    session_id = SessionId.new()
    terminal = _terminal(tmp_path, session_id, _TRUST)
    _give(tmp_path, session_id, mark)

    assert (await _settle(terminal, session_id)).awaiting_trust
    assert (await terminal.confirm_ready(session_id, _CLAUDE)).awaiting_trust


async def test_a_dead_pane_is_not_ready_whatever_its_mark_says(tmp_path: Path) -> None:
    session_id = SessionId.new()
    terminal = _terminal(tmp_path, session_id, _NO_BANNER, alive=False)
    _give(tmp_path, session_id, "started")

    assert not (await _settle(terminal, session_id)).live
    rechecked = await terminal.confirm_ready(session_id, _CLAUDE)
    assert not rechecked.live
    assert rechecked.detail == TERMINAL_NOT_LIVE
