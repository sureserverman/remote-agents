"""Every surface renders exactly `available_actions(state)` — no more, no less.

This is the test that catches a surface drifting from the shared policy. It is written
against the *rendered* buttons rather than the policy call, because a surface that asks the
policy and then adds or filters a button afterwards is precisely the defect the two former
Telegram copies were. Stage 3 extends the SURFACES tuple with the terminal.

What this test does NOT check: whether the policy itself is right. Both sides of the
assertion derive from `available_actions`, so changing it moves them together and this file
stays green — verified by mutation, not assumed. The policy's own correctness is pinned by
the hardcoded table in `tests/unit/application/test_session_actions.py`, which is the only
place a state's classification is written down independently. Keep it that way: replacing
that table with a call to `available_actions` would leave the classification untested
everywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.session_actions import available_actions
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_ACTION_LABELS = {"Graceful": "graceful", "Cleanup": "cleanup", "Force": "force"}


def _record(state: SessionState) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


class _Launcher:
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return (self.record,)

    async def inspect(self, _query):
        return None


async def _telegram_rendered_actions(record: SessionRecord) -> set[str]:
    """The stop actions the bot's detail view actually puts on screen."""
    boundary = PrivateBotBoundary(7, 11, launcher=_Launcher(record))
    await boundary._home_reply()
    detail = await boundary._detail_reply(str(record.session_id))
    return {
        _ACTION_LABELS[button.text]
        for row in detail.keyboard
        for button in row
        if button.text in _ACTION_LABELS
    }


async def _tui_rendered_actions(record: SessionRecord) -> set[str]:
    """The stop actions the local terminal's detail view actually puts on screen."""
    from remote_agents.adapters.tui.app import _ACTION_LABELS, RemoteAgentsTui
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    class _Launcher:
        async def refresh_readiness(self):
            return (record,)

        async def list_sessions(self):
            return (record,)

        async def copy_attach(self, _session_id):
            return None

    label_to_action = {label: action for action, label in _ACTION_LABELS.items()}
    app = RemoteAgentsTui(
        TuiContext(
            launcher=_Launcher(),  # type: ignore[arg-type]
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=tuple,
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        rows = [str(item.query_one("Label").content) for item in app.query("ListView > ListItem")]
    return {label_to_action[row] for row in rows if row in label_to_action}


# Both surfaces are pinned here. The parametrization is what makes adding a surface without
# pinning it to the policy impossible to do quietly.
SURFACES = (
    ("telegram", _telegram_rendered_actions),
    ("tui", _tui_rendered_actions),
)


@pytest.mark.parametrize("surface_name,render", SURFACES)
@pytest.mark.parametrize("state", list(SessionState))
async def test_surface_renders_exactly_the_policy_actions(
    surface_name: str, render, state: SessionState
) -> None:
    rendered = await render(_record(state))
    assert rendered == set(available_actions(state)), (
        f"{surface_name} diverged from the policy at state {state.value}"
    )


@pytest.mark.parametrize("surface_name,render", SURFACES)
async def test_surface_adds_no_action_of_its_own(surface_name: str, render) -> None:
    for state in SessionState:
        rendered = await render(_record(state))
        assert rendered <= {"graceful", "cleanup", "force"}, surface_name


async def test_the_policy_is_actually_exercised_by_this_test() -> None:
    """Guards the parity assertion from passing vacuously on an all-empty render."""
    rendered = await _telegram_rendered_actions(_record(SessionState.RUNNING))
    assert rendered, "a RUNNING session must render at least one action"
