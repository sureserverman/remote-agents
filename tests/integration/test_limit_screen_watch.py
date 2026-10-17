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
from remote_agents.adapters.agents.registry import profile_reported_kinds
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


def _sentence(apostrophe: str = "’", at: str = "Sep 29, 2026 3:05 AM") -> str:
    return (
        f"■ You{apostrophe}ve hit your usage limit. Upgrade to Plus to continue using Codex"
        f" (https://chatgpt.com/explore/plus), or try again at {at}."
    )


def _codex_limit(apostrophe: str = "’") -> str:
    """Codex's idle screen with its limit sentence as the last thing it printed."""
    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    sentence = _sentence(apostrophe)
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


def _watcher(panes: _Panes, sessions: dict[str, str], history=None) -> LimitScreenWatcher:
    return LimitScreenWatcher(
        _Store(sessions),
        panes.capture,
        {"codex": CODEX, "cursor-agent": CURSOR, "claude": SimpleNamespace(markers=(".",))},
        now=lambda: _NOW,
        reported=profile_reported_kinds(),
        history=history,
    )


class _History:
    """The record's answer for every session: its newest kind and its newest stop's words."""

    def __init__(self, newest: ActivityKind | None, stop: str | None = None) -> None:
        self.newest = newest
        self.stop = stop

    async def newest_kind(self, session_id: str) -> ActivityKind | None:
        return self.newest

    async def newest_stop_detail(self, session_id: str) -> str | None:
        return self.stop


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


async def test_a_line_the_owner_typed_that_starts_with_the_sentence_is_not_a_stop() -> None:
    """Only the agent's own glyph may lead the sentence; the composer's `›` may not."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    typed = ["› You’ve hit your usage limit, here is what I pasted:", "  secret-looking-text"]
    panes.screens[_CODEX] = "\n".join([*lines[:composer], *typed, *lines[composer + 1 :]]) + "\n"

    assert await watcher.poll() == ()


@pytest.mark.parametrize("answer_lines", [3, 20])
async def test_an_answer_bullet_quoting_the_sentence_is_not_a_stop(answer_lines: int) -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    answer = ["• You’ve hit your usage limit means the plan's window is spent."]
    answer += [f"  more of the answer, line {n}" for n in range(answer_lines)]
    panes.screens[_CODEX] = "\n".join([*answer, "", *lines[composer:]]) + "\n"

    assert await watcher.poll() == ()


async def test_the_stop_s_sentence_well_above_the_last_output_is_not_a_stop() -> None:
    """The tail window, exercised on a line the anchor alone would accept."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    old = ["■ You’ve hit your usage limit."]
    old += [f"› prompt {n}" if n % 2 else f"  answer {n}" for n in range(20)]
    panes.screens[_CODEX] = "\n".join([*old, "", *lines[composer:]]) + "\n"

    assert await watcher.poll() == ()


async def test_a_second_stop_while_the_first_is_still_on_screen_is_a_new_stop() -> None:
    """The owner retried before the reset and Codex said it again: that is a second stop."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"})
    panes.screens[_CODEX] = _CODEX_IDLE
    await watcher.poll()

    lines = _CODEX_IDLE.rstrip("\n").split("\n")
    composer = next(i for i, line in enumerate(lines) if line.startswith("›"))
    once = ["■ You’ve hit your usage limit."]
    twice = [*once, "› try again", "■ You’ve hit your usage limit."]
    panes.screens[_CODEX] = "\n".join([*lines[:composer], *once, "", *lines[composer:]]) + "\n"
    assert len(await watcher.poll()) == 1
    panes.screens[_CODEX] = "\n".join([*lines[:composer], *twice, "", *lines[composer:]]) + "\n"
    assert len(await watcher.poll()) == 1
    assert await watcher.poll() == ()


@pytest.mark.parametrize(
    "stop",
    [_sentence(at="Oct 17, 2026 6:37 PM"), None],
    ids=["an-earlier-stop", "never-stopped"],
)
async def test_a_stop_on_screen_at_first_sight_is_reported_when_the_record_has_not_seen_it(
    stop: str | None,
) -> None:
    """The 2026-10-17 miss: the service restarted at 20:22:03, Codex stopped at 20:22:27, and the
    first pass at 20:22:33 found the stop already up. The newest recorded news was a question
    from an hour earlier, and the newest recorded stop named another retry time."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"}, _History(ActivityKind.NEEDS_ANSWER, stop))
    panes.screens[_CODEX] = _codex_limit()

    (stop_seen,) = await watcher.poll()
    assert stop_seen.session_id == _CODEX
    assert stop_seen.kind is ActivityKind.LIMIT_REACHED
    assert stop_seen.confidence is ActivityConfidence.INFERRED
    assert await watcher.poll() == (), "reported once, then the screen is seen as before"


async def test_a_new_stop_straight_after_a_recorded_one_is_reported() -> None:
    """Nudged, and stopped again before anything else was recorded: the words differ."""
    panes = _Panes()
    recorded = _History(ActivityKind.LIMIT_REACHED, _sentence(at="Sep 28, 2026 10:05 PM"))
    watcher = _watcher(panes, {_CODEX: "codex"}, recorded)
    panes.screens[_CODEX] = _codex_limit()

    assert len(await watcher.poll()) == 1


@pytest.mark.parametrize(
    "newest", [ActivityKind.LIMIT_REACHED, ActivityKind.COMPLETED, ActivityKind.NEEDS_ANSWER]
)
async def test_the_recorded_stop_still_on_screen_is_not_reported_again(
    newest: ActivityKind,
) -> None:
    """The release review's case: a stop recorded and nudged, a short answer that finished, and
    its sentence still in the tail under the newer row. Reporting it would nudge it twice."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"}, _History(newest, _sentence()))
    panes.screens[_CODEX] = _codex_limit()

    assert await watcher.poll() == ()
    assert await watcher.poll() == ()


@pytest.mark.parametrize(
    "recorded",
    [
        # Wrapped at another pane width: the join put a space inside a word.
        _sentence().replace("usage limit", "us age limit"),
        # Cut to the stored bound, marked by its ellipsis.
        _sentence()[:60] + "…",
    ],
    ids=["rewrapped", "cut"],
)
async def test_the_recorded_words_match_across_a_rewrap_and_a_cut(recorded: str) -> None:
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"}, _History(ActivityKind.COMPLETED, recorded))
    panes.screens[_CODEX] = _codex_limit()

    assert await watcher.poll() == ()


async def test_a_session_with_no_recorded_news_seeds_its_stop_on_screen() -> None:
    """A session that has recorded nothing gives no ground to call a stop on screen new."""
    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"}, _History(None))
    panes.screens[_CODEX] = _codex_limit()

    assert await watcher.poll() == ()


async def test_a_failed_read_of_the_record_keeps_the_stop_on_screen_unreported() -> None:
    class _Failing(_History):
        async def newest_stop_detail(self, session_id: str) -> str | None:
            raise RuntimeError("database locked")

    panes = _Panes()
    watcher = _watcher(panes, {_CODEX: "codex"}, _Failing(ActivityKind.NEEDS_ANSWER))
    panes.screens[_CODEX] = _codex_limit()

    assert await watcher.poll() == ()
    panes.screens[_CODEX] = _CODEX_IDLE
    assert await watcher.poll() == ()
    panes.screens[_CODEX] = _codex_limit()
    assert len(await watcher.poll()) == 1, "the watch itself carries on"
