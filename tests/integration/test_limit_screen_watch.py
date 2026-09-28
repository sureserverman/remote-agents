"""Codex and Cursor Agent publish no limit event, so their limit stop is read off the pane.

The screens are the ones Task 1.1 pinned: Cursor's captured limit screen, and Codex's sentence
as its binary spells it (the pane itself was not kept). The watch is edge-triggered per session
-- one stop per appearance -- matches only the agent's last output, and never looks at an agent
that reports its own limit (Claude's `StopFailure`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_agents.adapters.agents.codex.limit_screen import LIMIT_SCREEN as CODEX
from remote_agents.adapters.agents.cursor.limit_screen import LIMIT_SCREEN as CURSOR
from remote_agents.application.limit_stops import LimitScreenWatcher
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, LimitHit

_FIXTURES = Path(__file__).resolve().parents[1] / "provider_contract" / "fixtures"
_PANES = Path(__file__).resolve().parents[1] / "fixtures" / "panes"
_NOW = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)

_CODEX = "11111111-1111-4111-8111-111111111111"
_CURSOR = "22222222-2222-4222-8222-222222222222"
_CLAUDE = "33333333-3333-4333-8333-333333333333"

_CODEX_IDLE = (_PANES / "codex" / "idle_0158.txt").read_text(encoding="utf-8")
_CURSOR_LIMIT = (_FIXTURES / "cursor" / "limit_screen.txt").read_text(encoding="utf-8")
_CURSOR_IDLE = (_PANES / "cursor" / "idle.txt").read_text(encoding="utf-8")


def _codex_limit(apostrophe: str = "’") -> str:
    """Codex's idle screen with its limit sentence as the last thing it printed."""
    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    sentence = (
        f"■ You{apostrophe}ve hit your usage limit. Upgrade to Plus to continue using Codex"
        " (https://chatgpt.com/explore/plus), or try again at Sep 29, 2026 3:05 AM."
    )
    # Above the composer, where Codex prints an error into the transcript.
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    return "\n".join([*lines[:composer], sentence, "", *lines[composer:]]) + "\n"


class _Store:
    def __init__(self, sessions: dict[str, str]) -> None:
        self.sessions = sessions

    async def list(self, states):
        assert tuple(states) == (SessionState.RUNNING,)
        return [
            SimpleNamespace(session_id=SessionId.parse(key), profile_id=ProfileId(profile))
            for key, profile in self.sessions.items()
        ]


class _Panes:
    def __init__(self) -> None:
        self.screens: dict[str, str] = {}
        self.captured: list[str] = []

    async def capture(self, session_id: SessionId) -> str:
        key = str(session_id)
        self.captured.append(key)
        screen = self.screens[key]
        if isinstance(screen, Exception):
            raise screen
        return screen


def _watcher(panes: _Panes, sessions: dict[str, str]) -> LimitScreenWatcher:
    return LimitScreenWatcher(
        _Store(sessions),
        panes.capture,
        {"codex": CODEX, "cursor-agent": CURSOR, "claude": SimpleNamespace(markers=(".",))},
        now=lambda: _NOW,
    )


async def test_a_limit_screen_is_one_stop_per_appearance() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})

    panes.screens[_CODEX] = _CODEX_IDLE
    assert await watcher.poll() == ()  # the first look only learns what the pane shows

    panes.screens[_CODEX] = _codex_limit()
    (stop,) = await watcher.poll()
    assert stop.session_id == _CODEX
    assert stop.kind is ActivityKind.LIMIT_REACHED
    assert stop.confidence is ActivityConfidence.INFERRED
    assert stop.limit is None, "the activity pass classifies it, as it does every stop"
    assert "hit your usage limit" in (stop.detail or "")

    assert await watcher.poll() == (), "a screen that is still up is not a second stop"

    panes.screens[_CODEX] = _CODEX_IDLE
    assert await watcher.poll() == ()
    panes.screens[_CODEX] = _codex_limit()
    assert len(await watcher.poll()) == 1, "gone and back is a new stop"


async def test_codex_s_sentence_is_matched_with_either_apostrophe() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    panes.screens[_CODEX] = _codex_limit("'")
    assert len(await watcher.poll()) == 1


async def test_cursor_s_captured_limit_screen_is_a_stop() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CURSOR: "cursor-agent"})
    panes.screens[_CURSOR] = _CURSOR_IDLE
    await watcher.poll()

    panes.screens[_CURSOR] = _CURSOR_LIMIT
    (stop,) = await watcher.poll()
    assert "out of usage" in (stop.detail or "")


async def test_a_marker_quoted_above_the_agent_s_last_output_is_not_a_stop() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    quoted = ["› what does “You’ve hit your usage limit” mean?", ""]
    quoted += [f"  • Line {n} of an answer about rate limits." for n in range(20)]
    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    panes.screens[_CODEX] = "\n".join([*quoted, *lines[composer:]]) + "\n"

    assert await watcher.poll() == ()


async def test_an_agent_that_reports_its_own_limit_is_never_captured() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CLAUDE: "claude", _CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE

    await watcher.poll()

    assert panes.captured == [_CODEX]


async def test_a_capture_that_fails_costs_that_pass_and_nothing_else() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex", _CURSOR: "cursor-agent"})
    panes.screens[_CODEX] = _CODEX_IDLE
    panes.screens[_CURSOR] = _CURSOR_IDLE
    await watcher.poll()

    panes.screens[_CODEX] = OSError("tmux went away")
    panes.screens[_CURSOR] = _CURSOR_LIMIT
    (stop,) = await watcher.poll()
    assert stop.session_id == _CURSOR


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "■ You’ve hit your usage limit. Try again at Sep 29, 2026 3:05 AM.",
            LimitHit(None, datetime(2026, 9, 29, 3, 5).astimezone()),
        ),
        ("■ You’ve hit your usage limit.", LimitHit(None, None)),
        ("Ran the suite.", None),
    ],
)
def test_codex_s_sentence_names_its_retry_instant_in_host_time(text, expected) -> None:
    assert CODEX.hint(text, _NOW) == expected


def test_cursor_s_stop_is_its_monthly_window() -> None:
    assert CURSOR.hint(_CURSOR_LIMIT, _NOW) == LimitHit("month", None)
    assert CURSOR.hint("Plan, search, build anything", _NOW) is None


async def test_a_wrapped_sentence_keeps_the_retry_instant_on_its_continuation_line() -> None:
    """Codex's long sentence wraps in an ordinary pane, and the time lands on the next line."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    wrapped = [
        "■ You’ve hit your usage limit. Upgrade to Plus to continue using Codex",
        "  (https://chatgpt.com/explore/plus), or try again at Sep 29,",
        "  2026 3:05 AM.",
    ]
    panes.screens[_CODEX] = "\n".join([*lines[:composer], *wrapped, "", *lines[composer:]]) + "\n"

    (stop,) = await watcher.poll()
    assert CODEX.hint(stop.detail or "", _NOW) == LimitHit(
        None, datetime(2026, 9, 29, 3, 5).astimezone()
    )


async def test_a_marker_inside_a_line_the_owner_typed_is_not_a_stop() -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    typed = "› Why does it say You’ve hit your usage limit when I have credits?"
    panes.screens[_CODEX] = "\n".join([*lines[:composer], typed, "", *lines[composer:]]) + "\n"

    assert await watcher.poll() == ()
