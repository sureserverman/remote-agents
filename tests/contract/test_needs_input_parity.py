"""A needs-input notice from the `claude agents` view says where it came from, on both surfaces.

The view fires `Notification` / `agent_needs_input` for a background agent it lists, and the
record lands on the session hosting the view, not on the agent that is blocked (BL-132). Its
detail is the agent's label, which for an unnamed background session is the opening words of
the owner's own prompt. So the line must say it came from the agents view, or it reads as the
hosting session asking. The owner chose to keep the label and say so (2026-11-18; DEC-037 and
DEC-098, as amended).

Driven from the spool through the drain, then into each surface's own renderer, so a change
at either end that drops the words turns this red.
"""

from __future__ import annotations

import json
from pathlib import Path

from remote_agents.adapters.telegram.notifications import render_activity
from remote_agents.adapters.tui.screens.feed import feed_rows
from remote_agents.application.activity import drain_activity
from remote_agents.application.notification_policy import SessionGroup
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS, AgentActivity

_SESSION = "01234567-89ab-cdef-0123-456789abcdef"
_WORDS = "from the agents view"
_NEEDS = "fix the build needs your input: which branch?"


def _drained(directory: Path, **fields: object) -> AgentActivity:
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "session_id": _SESSION,
        "event": "Notification",
        "reason": "agent_needs_input",
        "detail": _NEEDS,
        "observed_at": "2026-11-19T07:00:00+00:00",
        **fields,
    }
    (directory / f"{_SESSION}-1.json").write_text(json.dumps(record), encoding="utf-8")
    (activity,) = drain_activity(directory)
    return activity


def _bot(activity: AgentActivity) -> str:
    group = SessionGroup(activity.session_id, (activity,))
    return render_activity(
        group, display="atlas · claude · #4", open_session="c1_open_session_token"
    ).text


def _console(activity: AgentActivity, width: int = 200) -> str:
    ((_key, row, _disabled), *_rest) = feed_rows((activity,), width=width)
    return row.plain


def test_a_needs_input_notice_says_it_came_from_the_agents_view_on_both_surfaces(
    tmp_path: Path,
) -> None:
    activity = _drained(tmp_path)

    for surface, text in (("bot", _bot(activity)), ("console", _console(activity))):
        assert _WORDS in text, f"{surface}: {text!r}"
        assert _NEEDS in text, f"{surface}: {text!r}"


def test_a_long_label_still_says_where_it_came_from(tmp_path: Path) -> None:
    """The console cuts a detail at its own width, so the words lead the line."""
    long_label = "refactor " * 60 + "needs your input: which branch?"
    activity = _drained(tmp_path, detail=long_label)

    assert activity.detail is not None
    assert len(activity.detail) <= MAXIMUM_DETAIL_CHARACTERS
    assert _WORDS in _bot(activity)
    assert _WORDS in _console(activity, width=80)


def test_a_permission_request_line_is_unchanged(tmp_path: Path) -> None:
    activity = _drained(
        tmp_path, event="PermissionRequest", reason=None, detail="rm -rf build", ask="Bash"
    )

    assert activity.detail == "rm -rf build"
    assert "agents view" not in _bot(activity)
    assert "agents view" not in _console(activity)
