"""Every action a surface renders is exactly the set `available_actions(state)` allows.

This is the test that catches a surface drifting from the shared policy. It is written
against the *rendered* buttons rather than the policy call, because a surface that asks the
policy and then adds or filters a button afterwards is precisely the defect the two former
Telegram copies were. Both surfaces are now pinned; see SURFACES below.

The limit of that claim is `_LABEL_TO_ACTION`. Comparing a rendered surface to a set of
action ids means decoding each label on screen back through `ACTION_LABELS`, and a row whose
label is not a known action label decodes to nothing — it is filtered out of the rendered
set before the comparison, so it cannot make the equality fail. What this test therefore
catches is a surface rendering the *wrong* action: a row that collides with a known action
label, in a state whose policy does not permit it, or a permitted row that is missing. What
it does not catch is a surface growing an extra row whose label collides with nothing —
another button, a menu entry, a heading — because such a row is invisible on both sides of
the assertion. That is a deliberate limit and not an oversight; see DEC-019, which declined
an allow-list of recognized rows on the grounds that it must be kept current and fails
noisily when it is not.

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
from textual.widgets import OptionList

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.session_actions import ACTION_LABELS, available_actions
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

# One decoder for both surfaces. This used to be a hand-written table mapping the bot's
# title-cased spellings back to action ids, which existed only because the two surfaces
# named the same buttons differently — the drift this file is meant to catch, sitting
# unremarked in its own fixtures. Both now render `ACTION_LABELS`, so decoding is its
# inverse and a surface inventing a label of its own falls out of the sets below.
_LABEL_TO_ACTION = {label: action for action, label in ACTION_LABELS.items()}


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
        _LABEL_TO_ACTION[button.text]
        for row in detail.keyboard
        for button in row
        if button.text in _LABEL_TO_ACTION
    }


async def _tui_rendered_actions(record: SessionRecord) -> set[str]:
    """The stop actions the local terminal's detail view actually puts on screen."""
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    class _Launcher:
        async def refresh_readiness(self):
            return (record,)

        async def list_sessions(self):
            return (record,)

        async def copy_attach(self, _session_id):
            return None

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
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        # Still the *rendered* rows, which is this file's whole premise (see the module
        # docstring). The widget changed from a list of mounted `Label`s to an `OptionList`,
        # so the read is `Option.prompt` off `.options` rather than a DOM query — but it is
        # the same question asked of the same artifact: what the detail view actually put on
        # screen. Reading `_detail_entries` or `available_actions` here instead would satisfy
        # every assertion below vacuously, for every state, while catching nothing.
        #
        # Read off `app.screen` rather than `app` since the surface gained a real screen
        # stack. That is a strengthening, not a workaround: `App.query_one` resolves against
        # the *bottom* of the stack, so with the detail pushed on top the old spelling would
        # have returned the project list's rows — every state would render zero actions and
        # the equality below would hold vacuously for the states whose policy set is empty.
        # `app.screen` is the position actually on screen, which is what this file asks about.
        choices = app.screen.query_one("#choices", OptionList)
        rows = [str(option.prompt) for option in choices.options]
    return {_LABEL_TO_ACTION[row] for row in rows if row in _LABEL_TO_ACTION}


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


@pytest.mark.parametrize("surface_name,render", SURFACES)
async def test_the_policy_is_actually_exercised_by_this_test(surface_name: str, render) -> None:
    """Guards the parity assertion from passing vacuously on an all-empty render.

    Parametrized over both surfaces: a renderer that silently returned nothing would
    satisfy the equality above for every state whose policy set is empty.
    """
    rendered = await render(_record(SessionState.RUNNING))
    assert rendered, f"{surface_name}: a RUNNING session must render at least one action"
