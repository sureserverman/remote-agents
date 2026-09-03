"""Every action a surface renders is exactly the set `available_actions(state, provenance)` allows.

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

**This file carries a second contract the paragraphs above do not mention.**
`test_both_surfaces_offer_the_same_remote_control_directions` compares the Remote Control
direction rows across both surfaces, against `remote_control_directions`, and it fails
separately from everything described so far. Named here because a reader taking this
docstring as the file's inventory would not know that check lives in it — the understatement
predates the shared-use-cases sub-plan and survived its Task 2.4 re-read, and was found by
the Stage 2 gate's evaluator.

What this test does NOT check: whether the policy itself is right. Both sides of the
assertion derive from `available_actions`, so changing it moves them together and this file
stays green — verified by mutation, not assumed. The policy's own correctness is pinned by
the hardcoded table in `tests/unit/application/test_session_actions.py`, which is the only
place a state's classification is written down independently. Keep it that way: replacing
that table with a call to `available_actions` would leave the classification untested
everywhere.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from surfaces import surface_pairs
from textual.widgets import OptionList

from remote_agents.adapters.telegram.service import build_private_bot, unmarked
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS as _HOST_LABELS,
)
from remote_agents.application.host_remote_control import (
    host_remote_control_directions,
    pair_available,
)
from remote_agents.application.session_actions import ACTION_LABELS, available_actions
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)

# One decoder for both surfaces. This used to be a hand-written table mapping the bot's
# title-cased spellings back to action ids, which existed only because the two surfaces
# named the same buttons differently — the drift this file is meant to catch, sitting
# unremarked in its own fixtures. Both now render `ACTION_LABELS`, so decoding is its
# inverse and a surface inventing a label of its own falls out of the sets below.
_LABEL_TO_ACTION = {label: action for action, label in ACTION_LABELS.items()}


def _record(
    state: SessionState, orphan_provenance: OrphanProvenance | None = None
) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        state,
        datetime.now(UTC),
        orphan_provenance=orphan_provenance,
    )


# A *situation*, not a state. DEC-020 split ORPHANED into two, and only one of them offers a
# destructive row -- so a parametrization over `SessionState` alone would leave the branch
# that carries a kill button compared on neither surface. That is exactly the divergence this
# file exists to catch, and it would have been invisible to it.
SITUATIONS: list[tuple[SessionState, OrphanProvenance | None]] = [
    *((state, None) for state in SessionState),
    (SessionState.ORPHANED, OrphanProvenance.AMBIGUOUS),
    (SessionState.ORPHANED, OrphanProvenance.ADOPTED),
]


class _Launcher(SessionUseCaseDouble):
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return (self.record,)

    async def inspect(self, _query):
        return None


async def _telegram_rendered_actions(record: SessionRecord) -> set[str]:
    """The stop actions the bot's detail view actually puts on screen."""
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=_Launcher(record)))
    detail = await boundary._detail_reply(str(record.session_id))
    # `unmarked` strips the bot's own mark (`⏹ Stop and close` → `Stop and close`): the mark is
    # this surface's presentation and the word behind it is what the policy pins.
    return {
        _LABEL_TO_ACTION[unmarked(button.text)]
        for row in detail.keyboard
        for button in row
        if unmarked(button.text) in _LABEL_TO_ACTION
    }


async def _tui_rendered_actions(record: SessionRecord) -> set[str]:
    """The stop actions the local terminal's detail view actually puts on screen."""
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    class _Launcher(SessionUseCaseDouble):
        async def refresh_readiness(self):
            return (record,)

        async def list_sessions(self):
            return (record,)

        async def copy_attach(self, _session_id):
            return None

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=_Launcher(),  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
            ),
            profiles=(ProfileAvailability("claude", True),),
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
SURFACES = surface_pairs(telegram=_telegram_rendered_actions, tui=_tui_rendered_actions)


@pytest.mark.parametrize("surface_name,render", SURFACES)
@pytest.mark.parametrize(("state", "provenance"), SITUATIONS)
async def test_surface_renders_exactly_the_policy_actions(
    surface_name: str, render, state: SessionState, provenance: OrphanProvenance | None
) -> None:
    rendered = await render(_record(state, provenance))
    assert rendered == set(available_actions(state, provenance)), (
        f"{surface_name} diverged from the policy at state {state.value} "
        f"with provenance {provenance}"
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


async def _telegram_remote_control(record: SessionRecord) -> list[str]:
    """The Remote Control rows the bot's detail view actually puts on screen."""
    from remote_agents.application.profiles import ProfileAvailability

    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(sessions=_Launcher(record)),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    detail = await boundary._detail_reply(str(record.session_id))
    return [
        unmarked(button.text)
        for row in detail.keyboard
        for button in row
        if "Remote Control" in button.text
    ]


async def _terminal_remote_control(record: SessionRecord) -> list[str]:
    """The same rows on the local surface, read from its own entry table."""
    from remote_agents.adapters.tui.screens.sessions import remote_control_entries

    return [label for _key, label in remote_control_entries(record)]


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (None, ["Remote Control on", "Remote Control off"]),
        (RemoteControlState.ACTIVE, ["Remote Control off"]),
        (RemoteControlState.INACTIVE, ["Remote Control on"]),
    ],
)
@pytest.mark.parametrize(
    ("surface", "rows"),
    [("telegram", _telegram_remote_control), ("terminal", _terminal_remote_control)],
)
async def test_both_surfaces_offer_the_same_remote_control_directions(
    observed, expected, surface, rows
) -> None:
    """One observation, one answer, on both surfaces.

    Unknown offers both, which is what every surface did before the state was stored — so the
    fallback is the old behaviour rather than a new way to hide the action the owner needs.
    """
    record = replace(_record(SessionState.RUNNING), remote_control_state=observed)

    assert await rows(record) == expected, f"{surface} disagrees for observed={observed}"


# --- The host action -------------------------------------------------------------------
#
# A second `surface_pairs`, because the host toggle is a second vocabulary the two surfaces
# must render identically -- and it is a *different* one. Everything above is keyed by a
# `SessionRecord` and answers "what may this session be asked to do"; nothing here has a
# session at all. The pairing was written twice rather than generalised for the same reason
# `HostRemoteControlCommand` is a separate type: a record that is sometimes meaningful is a
# field every reader has to ask about.


async def _telegram_host_offer(status) -> tuple[frozenset[str], bool]:
    """What the bot puts on screen for this reading: directions offered, and pairing."""
    from backends import FakeHostRemoteControl

    from remote_agents.adapters.telegram.presenters import unpadded

    control = FakeHostRemoteControl(status.connection)
    bot = build_private_bot(7, 11, backend=backend_for(host_remote_control=control))
    screen = await bot._host_remote_control_reply()
    labels = [unmarked(unpadded(button.text)) for row in screen.keyboard for button in row]
    words = frozenset(_HOST_LABELS.values())
    offered = frozenset(label for label in labels if label in words)
    return offered, any("Pair" in label for label in labels)


class _HostLauncher(SessionUseCaseDouble):
    """Just enough session use case for the dashboard to draw itself without complaining."""

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self):
        return ()


async def _tui_host_offer(status) -> tuple[frozenset[str], bool]:
    """What the terminal offers for this reading, observed by driving the real flow.

    **Deliberately not `screen.host_directions_offered(status)`.** That accessor returns the
    policy, so a surface that consulted it and then declined to act would still report the
    full set -- which is exactly what happened: an early `return` after the call passed every
    assertion in this file while the terminal offered nothing. A parity test that asks a
    surface what it would do, rather than watching what it does, cannot see the divergence it
    exists to catch.

    So this drives `confirm_host_remote_control` and reads the modal that actually appears:
    the confirmation names one direction, the chooser offers several, and neither appearing
    means none were offered.
    """
    import asyncio

    from backends import FakeHostRemoteControl
    from textual.widgets import OptionList as _OptionList

    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.adapters.tui.screens.confirm import (
        HostRemoteControlConfirmModal,
        HostRemoteControlDirectionModal,
    )
    from remote_agents.application.host_remote_control import HOST_REMOTE_CONTROL_LABELS
    from remote_agents.application.profiles import ProfileAvailability

    words = frozenset(HOST_REMOTE_CONTROL_LABELS.values())
    control = FakeHostRemoteControl(status.connection)
    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=_HostLauncher(),
                projects=object(),
                refresh_catalogue=tuple,
                host_remote_control=control,
            ),
            profiles=(ProfileAvailability("codex", True),),
            attach_argv=lambda session_id: ("tmux",),
        )
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        dashboard = app.screen

        asking = asyncio.create_task(dashboard.confirm_host_remote_control())
        offered: frozenset[str] = frozenset()
        try:
            for _ in range(60):
                await pilot.pause()
                screen = app.screen
                if isinstance(screen, HostRemoteControlDirectionModal):
                    options = screen.query_one("#choices", _OptionList)
                    offered = (
                        frozenset(
                            str(options.get_option_at_index(index).prompt)
                            for index in range(options.option_count)
                        )
                        & words
                    )
                    break
                if isinstance(screen, HostRemoteControlConfirmModal):
                    # The direction is in the confirm row ("Yes, remote control off"): the
                    # question above names the fact, the row names the act.
                    options = screen.query_one("#choices", _OptionList)
                    rendered = " ".join(
                        str(options.get_option_at_index(index).prompt)
                        for index in range(options.option_count)
                    ).casefold()
                    offered = frozenset(word for word in words if word.casefold() in rendered)
                    break
            await pilot.press("escape")
            await pilot.pause()
        finally:
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)

        pairing = asyncio.create_task(dashboard.confirm_host_pair())
        try:
            for _ in range(60):
                await pilot.pause()
                if control.calls.count("pair"):
                    break
            paired = control.calls.count("pair") > 0
            if paired:
                await pilot.press("escape")
                await pilot.pause()
        finally:
            pairing.cancel()
            await asyncio.gather(pairing, return_exceptions=True)

    return offered, paired


HOST_SURFACES = surface_pairs(telegram=_telegram_host_offer, tui=_tui_host_offer)


@pytest.mark.parametrize("surface_name,offer", HOST_SURFACES)
@pytest.mark.parametrize("connection", list(HostConnection), ids=lambda c: c.value)
async def test_both_surfaces_offer_exactly_the_host_policy_s_directions(
    surface_name: str, offer, connection: HostConnection
) -> None:
    """DEC-007 for the host action: one policy, one vocabulary, two renderers.

    Written over every `HostConnection` rather than over the interesting ones, because the
    readings that diverged in practice were the two nobody thought were interesting --
    `ERRORED` and `UNREACHABLE`, where the policy declines to say which way the host is set
    and therefore offers both.
    """
    status = HostRemoteControlStatus.observed(connection, server_name=None)
    expected = frozenset(
        _HOST_LABELS[direction] for direction in host_remote_control_directions(status)
    )
    offered, _ = await offer(status)
    assert offered == expected, f"{surface_name} disagrees with the policy on {connection}"


@pytest.mark.parametrize("surface_name,offer", HOST_SURFACES)
@pytest.mark.parametrize("connection", list(HostConnection), ids=lambda c: c.value)
async def test_both_surfaces_offer_pairing_under_the_same_predicate(
    surface_name: str, offer, connection: HostConnection
) -> None:
    status = HostRemoteControlStatus.observed(connection, server_name=None)
    _, pairing = await offer(status)
    assert pairing is pair_available(status), (
        f"{surface_name} offers pairing where the policy does not, on {connection}"
    )
