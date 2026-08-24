"""The right-top pane: every managed session, and the pane a session is opened from.

The sessions pane is the swap controller deliberately. It is the one pane that stays on
screen once an agent occupies the left slot, so it is the only place the owner can reach
back from — which is why Enter here means *exchange this agent into the left pane*, and the
detail, where every stop, inspect, rename and Remote Control affordance lives, is one key
away (DEC-007: the full action set stays reachable; opening narrows nothing).

The list, its own refresh cadence, the stale-read guards and the empty state are inherited
from `SessionsScreen` rather than re-implemented — what this pane changes is what Enter
means. On the combined dashboard Enter on a session row already opened the session and `d`
already opened the detail; this is that pair, on a screen of its own.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, tui_context_for
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import SessionsPane
from remote_agents.adapters.tui.screens.sessions import SessionDetailScreen, SessionsPaneScreen
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_OTHER = SessionId.parse("fedcba98-7654-3210-fedc-ba9876543210")


class _Launcher(SessionUseCaseDouble):
    def __init__(self, records: tuple[SessionRecord, ...]) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record(session_id: SessionId = _SESSION, name: str = "existing") -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(name, "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _context(records: tuple[SessionRecord, ...] = (), **overrides) -> TuiContext:
    base = {
        "sessions": _Launcher(records),
        "projects": object(),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_PROJECT,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_PROJECT,),
        # Wired because `i` is offered only where there is something to inspect, exactly as
        # `p` is offered only where a console is wired. The real composition always wires it.
        "capture": lambda _session_id: "captured output",
    }
    base.update(overrides)
    return tui_context_for(**base)


def test_the_sessions_pane_rests_on_the_sessions_list() -> None:
    assert isinstance(SessionsPane(_context()).get_default_screen(), SessionsPaneScreen)


async def test_enter_on_a_row_issues_one_show_and_the_pane_stays() -> None:
    """Enter exchanges the agent into the left slot; the pane it was pressed from remains.

    One call, not two: the pane is the controller and a doubled exchange would swap the
    agent in and straight back out again.
    """
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    app = SessionsPane(_context((_record(),), open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.highlighted == 0
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert shown == [str(_SESSION)]
        assert app.is_running, "opening a session never ends the pane that opened it"
        assert position(app) == "SESSIONS_PANE"


async def test_enter_opens_the_row_the_cursor_is_on_not_the_first_one() -> None:
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    records = (_record(), _record(_OTHER, "other"))
    app = SessionsPane(_context(records, open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert shown == [str(_OTHER)]


async def test_the_detail_key_opens_the_detail_with_no_prior_arrow_press() -> None:
    """DEC-007's full action set is one key away, and it answers a bare key.

    A bare `d` rather than a hand-set `highlighted`: a pane advertising a key that only works
    after an arrow press makes it a silent no-op, which is invisible to a test that sets the
    cursor itself.
    """
    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#choices", OptionList).focus()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, SessionDetailScreen)


async def test_an_empty_list_declares_its_state() -> None:
    """DEC-009: this pane can be empty, and says so rather than showing nothing."""
    app = SessionsPane(_context(()))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 1
        option = choices.get_option_at_index(0)
        assert option.disabled is True
        assert "No managed sessions on this host." in str(option.prompt)


async def test_the_cursor_rests_painted_on_a_row_whose_enter_does_not_mutate() -> None:
    """BL-004's constraint, as it lands on this pane.

    The resting row's Enter must not mutate, and here it does not: Enter *opens* — an
    exchange of panes, which writes no record and touches no lifecycle (DEC-040). Every
    mutating action lives behind `d`, on the detail. So the cursor may rest on the first
    session row, which is what the combined dashboard's pane already does, and the row it
    rests on must be *painted* rather than merely indexed — the distinction
    `test_resting_cursor.py` exists for.
    """
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    app = SessionsPane(_context((_record(),), open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.highlighted == 0
        cursor = choices.get_visual_style(
            "option-list--option", "option-list--option-highlighted"
        ).rich_style.clear_meta_and_links()
        painted = [
            segment
            for line in range(choices.scrollable_content_region.height)
            for segment in choices.render_line(line)
            if segment.text.strip() and segment.style is not None
        ]
        assert any(segment.style.clear_meta_and_links() == cursor for segment in painted), (
            "the sessions pane drew no cursor on its resting row"
        )

        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert shown == [str(_SESSION)], "the resting row's enter must open, never mutate"


async def test_the_pane_re_reads_on_its_own_cadence() -> None:
    """The store has a second writer, so this pane goes stale with nobody touching it."""
    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.screen._auto is not None, "the sessions pane must poll its own list"


async def test_without_a_console_capability_opening_still_leaves_by_attach() -> None:
    """A pane run outside the console keeps the exec-attach contract exactly as it was."""
    app = SessionsPane(_context((_record(),), open_in_console=None))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is not None
    assert app.return_value.session_id == str(_SESSION)


async def test_the_console_capability_the_composition_wires_is_the_exchange() -> None:
    """The wiring itself, at the seam the composition root owns.

    Asserted against the executed capability rather than bootstrap's source text, for the
    reason `test_tui_bootstrap.py` records: a substring check for the same wiring matched the
    *service* composition too, so deleting it from the local one left the suite green.

    DEC-039's accepted cost 1 names this replacement by hand — under the swap model the
    console reaches an agent through `ConsoleComposer.show`, and Sub-plan 3 wires that in
    place of the switch route.
    """
    from remote_agents.bootstrap import _console_opener

    class _Composer:
        def __init__(self) -> None:
            self.shown: list[SessionId] = []
            self.opened: list[SessionId] = []

        async def show(self, session_id: SessionId) -> None:
            self.shown.append(session_id)

        async def open(self, session_id: SessionId) -> None:  # pragma: no cover - must not run
            self.opened.append(session_id)

    composer = _Composer()
    await _console_opener(composer)(str(_SESSION))
    assert composer.shown == [_SESSION]
    assert composer.opened == []


async def test_a_show_that_fails_leaves_the_pane_running_and_says_so() -> None:
    """Presentation degrades; the pane never dies because an exchange did not happen."""

    async def refuse(session_id: str) -> None:
        raise RuntimeError("the console is wedged")

    app = SessionsPane(_context((_record(),), open_in_console=refuse))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
        assert position(app) == "SESSIONS_PANE"


async def test_the_status_names_what_enter_actually_does_here() -> None:
    """Inherited, both sentences described the *dashboard's* keys.

    Found by driving the real pane at the Stage 1 gate: it read "Select one for detail",
    which is what Enter means on the sessions screen the dashboard pushes and not what it
    means here. A status that names the wrong key is a false sentence, and it is the kind
    only a live drive shows.
    """
    from textual.widgets import Static

    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        status = str(app.screen.query_one("#status", Static).content)
        assert "Enter opens one" in status
        assert "d for its detail" in status
        assert "Select one for detail" not in status


async def test_an_empty_pane_does_not_offer_an_escape_it_does_not_have() -> None:
    """This pane is its process's resting position, so escape at rest is inert.

    The inherited sentence sent the owner to a project list that does not exist in this
    process — a dead end dressed as an instruction.
    """
    from textual.widgets import Static

    app = SessionsPane(_context(()))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        status = str(app.screen.query_one("#status", Static).content)
        assert "escape" not in status.lower()
        assert "project list" not in status


async def test_a_failed_read_does_not_send_the_owner_somewhere_that_is_not_there() -> None:
    """The failure path kept the sentence the gate commit fixed everywhere else.

    `report_store_failure` renders onto the screen whose read failed. On this pane that
    screen is the process's resting position: `go_back` refuses to pop the last screen, so
    "Press escape to return to the project list" named an inert key and a position that does
    not exist in this process — and it drew a Back row that could not go back, at the moment
    the surface most needed to be honest. Found by the Stage 1 gate evaluator.
    """
    from textual.widgets import OptionList, Static

    class _Failing(_Launcher):
        async def list_sessions(self):
            raise RuntimeError("store contended")

    app = SessionsPane(_context((), sessions=_Failing(())))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # Through the screen's own reload, which is the path that catches and reports.
        await app.screen.reload()
        await pilot.pause()

        status = str(app.screen.query_one("#status", Static).content)
        assert "could not be read" in status, "the failure must still be named"
        assert "escape" not in status.lower()
        assert "project list" not in status
        assert "Ctrl+R" in status

        choices = app.screen.query_one("#choices", OptionList)
        rows = [str(option.prompt) for option in choices.options]
        assert "Back" not in rows, "a Back row that cannot go back is a key that does nothing"


async def test_the_pane_offers_no_flow_that_starts_by_choosing_a_project() -> None:
    """Carried from the Stage 1 gate: every pane inherited the whole app's bindings.

    All three flows — add project, resume, sessions — begin by choosing a project, which is
    the pane next door. Pushing the launch wizard in here would bury the list this pane
    exists to keep in sight, and "Sessions" is a key for reaching a list that is already on
    screen. Hidden *and* declined: this surface's rule is that a footer entry may only be
    hidden where the action it names already refuses to run.
    """
    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        offered = set(app.screen.active_bindings)
        assert {"ctrl+n", "ctrl+o", "ctrl+s"}.isdisjoint(offered), offered

        await app.action_add_project()
        await app.action_sessions()
        await pilot.pause()
        assert position(app) == "SESSIONS_PANE", "a declined flow must not move the pane"


async def test_a_session_that_cannot_be_shown_says_why_instead_of_doing_nothing() -> None:
    """The bug an owner actually hit: click a row, watch nothing happen.

    `ConsoleComposer.show` degrades to a log line by contract (DEC-040) and nothing in
    `src/` configures logging, so a session it declined to display was silence. The
    commonest reason is not a fault: a session launched before identity moved to the pane
    (DEC-038) names no pane, so there is nothing to exchange. It is still listed, stoppable
    and inspectable — it just cannot be shown, and now it says so and names the repair.
    """

    async def refuse(session_id: str) -> str:
        return "This session started before ... Run: remote-agents upgrade-sessions"

    app = SessionsPane(_context((_record(),), open_in_console=refuse))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()

        said = [str(note.message) for note in app._notifications]
        assert any("upgrade-sessions" in line for line in said), said
        assert app.is_running, "a refusal is not a reason to lose the pane"


async def test_a_session_that_is_shown_says_nothing_at_all() -> None:
    """Success is silent; only a refusal is worth interrupting for."""

    async def show(session_id: str) -> None:
        return None

    app = SessionsPane(_context((_record(),), open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert list(app._notifications) == []


# The project a row names ----------------------------------------------------------------------


async def test_a_row_names_its_project_rather_than_the_catalogue_id() -> None:
    """The defect this closes, captured from the live surface before the change:

        034b69be3a8290521db3d76e · codex · regular · #3 · running · 10d ago

    `SessionDisplayIdentity.project_slug` holds the catalogue's `opaque_id`, and the bot has
    always swapped it for the readable name at render time. This surface never did.
    """
    app = SessionsPane(_context((_record(name="opaque-existing"),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        row = choices.get_option_at_index(0)
        assert "existing" in str(row.prompt)
        assert "opaque-existing" not in str(row.prompt)


async def test_naming_the_project_leaves_the_row_key_alone() -> None:
    """The key is the handle every action screen is reached through.

    Getting the name wrong is cosmetic; getting the *key* wrong strands Stop, Force stop,
    Rename and Inspect behind a row that no longer addresses anything.
    """
    app = SessionsPane(_context((_record(name="opaque-existing"),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.get_option_at_index(0).id == str(_SESSION)


async def test_a_session_whose_project_left_the_catalogue_still_renders() -> None:
    """Deregistered, or a directory moved, while the session runs. The slug is then the only
    name there is, and a row the owner cannot see is a session they cannot stop."""
    app = SessionsPane(_context((_record(name="vanished"),), catalogue=()))
    async with app.run_test() as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 1
        assert "vanished" in str(choices.get_option_at_index(0).prompt)


# A key per action, each routed into the detail's own chain ------------------------------------

_ACTION_KEYS = (
    ("a", "attach"),
    ("i", "inspect"),
    ("r", "rename"),
    ("f", "force"),
)


@pytest.mark.parametrize(("key", "action"), _ACTION_KEYS)
async def test_each_key_pushes_the_detail_carrying_its_action(key: str, action: str) -> None:
    """DEC-007's control plane, one key deep instead of two.

    Each key names an action and hands it to `SessionDetailScreen`, which performs it through
    the chain it already has -- the same confirmations, refusals and guards a pressed row
    gets. The key itself decides nothing about whether the action is legal; that is the
    policy's answer, re-checked at issue time.
    """
    opened: list[tuple[str, str | None]] = []
    app = SessionsPane(_context((_record(),)))

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsPaneScreen)

        async def capture(session_value, opening_action=None):
            opened.append((session_value, opening_action))

        app.show_detail = capture  # type: ignore[method-assign]
        await pilot.press(key)
        await pilot.pause()

    assert opened == [(str(_SESSION), action)], opened


async def test_a_key_with_no_row_highlighted_is_a_no_op() -> None:
    """An empty list still has a footer. A key pressed against no row must do nothing rather
    than raise out of a binding, which on this surface exits the app."""
    opened: list = []
    app = SessionsPane(_context(()))

    async with app.run_test() as pilot:
        await pilot.pause()

        async def capture(*a, **k):
            opened.append(a)

        app.show_detail = capture  # type: ignore[method-assign]
        for key in ("a", "i", "r", "s", "c", "f", "m"):
            await pilot.press(key)
            await pilot.pause()
        assert app.is_running, "a key with no highlighted row took the surface down"

    assert opened == []


async def test_no_action_key_collides_with_an_app_level_binding() -> None:
    """The pane hides its filter, so bare letters are free here in a way they are not on the
    projects pane. What is *not* free is anything the app already binds -- a screen binding
    shadowing Quit or Back would take the key away everywhere it is inherited."""
    from remote_agents.adapters.tui.screens.sessions import SessionsPaneScreen as _Pane

    app_keys = {"escape", "ctrl+r", "ctrl+n", "ctrl+s", "ctrl+o", "ctrl+q"}
    pane_keys = {binding.key for binding in _Pane.BINDINGS}
    assert not (pane_keys & app_keys), sorted(pane_keys & app_keys)


async def test_no_trust_key_is_offered_dec_047() -> None:
    """DEC-047: the local surface answers the trust question in the pane the console exchanges
    in, so it deliberately has no trust row -- and must not grow a trust *key* either."""
    from remote_agents.adapters.tui.screens.sessions import SessionsPaneScreen as _Pane

    actions = " ".join(str(binding.action) for binding in _Pane.BINDINGS)
    assert "trust" not in actions.lower(), actions


async def test_m_performs_the_single_offered_direction() -> None:
    """Where the policy offers one direction, the key performs it."""
    from dataclasses import replace as _replace

    from remote_agents.domain.remote_control import RemoteControlState

    opened: list[tuple[str, str | None]] = []
    record = _replace(_record(), remote_control_state=RemoteControlState.INACTIVE)
    app = SessionsPane(_context((record,)))

    async with app.run_test() as pilot:
        await pilot.pause()

        async def capture(session_value, opening_action=None):
            opened.append((session_value, opening_action))

        app.show_detail = capture  # type: ignore[method-assign]
        await pilot.press("m")
        await pilot.pause()

    assert len(opened) == 1, opened
    assert opened[0][1] == "remote-control-active", opened


async def test_m_opens_the_detail_unmodified_when_the_direction_is_unknown() -> None:
    """`remote_control_directions` offers *both* when nobody has toggled this session or the
    observation came back UNKNOWN -- deliberately, because unknown must not be guessed at.

    A key that picked one would be answering, on a live pane, a question the policy declines
    to answer. So it opens the detail and lets the owner choose, which is the same two
    keypresses this key exists to save everywhere else and the right number here.
    """
    opened: list[tuple[str, str | None]] = []
    app = SessionsPane(_context((_record(),)))  # no remote_control_state observed

    async with app.run_test() as pilot:
        await pilot.pause()

        async def capture(session_value, opening_action=None):
            opened.append((session_value, opening_action))

        app.show_detail = capture  # type: ignore[method-assign]
        await pilot.press("m")
        await pilot.pause()

    assert opened == [(str(_SESSION), None)], opened


async def test_no_key_auto_performs_an_action_the_detail_would_not_ask_about() -> None:
    """The rule this task's Tier-1 review produced, pinned as a rule rather than as a list.

    `SessionDetailScreen.choose` asks before `force` and before either Remote Control
    direction, and does not ask before Stop and close or Clean up -- the branch
    `key in ACTION_LABELS and key != FORCE`. A key that auto-performed one of those would
    remove the only thing standing in front of it, which was two deliberate keypresses rather
    than a confirmation (DEC-018 declined confirmations for both, on both surfaces).

    That matters here specifically because this list restores its cursor *by key* every ten
    seconds and falls back to row 0 when a key has gone -- so a session ending between ticks
    moves the cursor to a different session with nothing said.

    Derived from the policy rather than hardcoded: an unconfirmed action added to
    `ACTION_LABELS` tomorrow is excluded the day it appears, instead of quietly becoming
    bindable.
    """
    from remote_agents.adapters.tui.screens.sessions import (
        SESSION_ACTION_KEYS,
        UNCONFIRMED_MUTATING_ACTIONS,
    )
    from remote_agents.application.session_actions import ACTION_LABELS, FORCE

    assert UNCONFIRMED_MUTATING_ACTIONS == frozenset(ACTION_LABELS) - {FORCE}
    assert UNCONFIRMED_MUTATING_ACTIONS, "the rule must not be vacuous"

    bound = {action for _key, action, _label in SESSION_ACTION_KEYS}
    offenders = bound & UNCONFIRMED_MUTATING_ACTIONS
    assert not offenders, (
        f"these keys auto-perform an action the detail never asks about: {sorted(offenders)}"
    )


async def test_both_sessions_positions_offer_the_same_action_keys() -> None:
    """`SessionsScreen` and `SessionsPaneScreen` each declare `BINDINGS` from the same list,
    and both carry the same 10-second auto-refresh -- so both are the surface this task's
    Critical was about, and an edit touching one alone must not go unnoticed.

    Asserted as an equality between the two key sets rather than as two separate lists, so the
    sharing itself is the pinned invariant.
    """
    from remote_agents.adapters.tui.screens.sessions import SessionsScreen

    action_keys = {key for key, _a, _l in _module_action_keys()} | {"m"}
    full = {str(b.key) for b in SessionsScreen.BINDINGS}
    pane = {str(b.key) for b in SessionsPaneScreen.BINDINGS}
    assert action_keys <= full, sorted(action_keys - full)
    assert action_keys <= pane, sorted(action_keys - pane)
    # The pane adds `d` and `p`; every row key is shared.
    #
    # `p` is the pane's alone on purpose. Hosting is decided by the tmux socket name, so a
    # plain `remote-agents tui` started from any shell on the console's server is classified
    # CONSOLE and has `console_show_projects` wired -- and a `p` on the full sessions position
    # would then rearrange the owner's real console from a process that is not one of its
    # managed panes. This asserts the separation rather than leaving it to the comment.
    assert pane - full == {"d", "p"}, sorted(pane - full)
    assert "p" not in full, "the full sessions position must not offer the console key"


def _module_action_keys():
    from remote_agents.adapters.tui.screens.sessions import SESSION_ACTION_KEYS

    return SESSION_ACTION_KEYS


async def test_the_merged_keymap_is_what_carries_the_action_keys() -> None:
    """Asserted against the *effective* bindings, not a class's declared list.

    `SessionsPaneScreen.BINDINGS` repeats `*SESSION_ACTION_BINDINGS` rather than relying on
    Textual's MRO merge, and a future reader may reasonably delete the repetition as redundant
    -- it is. Reading the declared list would then silently stop checking the inherited keys
    and still pass, so this reads what the mounted screen actually offers.
    """
    app = SessionsPane(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        offered = set(app.screen.active_bindings)

    from remote_agents.adapters.tui.screens.sessions import SESSION_ACTION_KEYS

    for key, _action, _label in SESSION_ACTION_KEYS:
        assert key in offered, f"{key!r} is not in the screen's effective keymap: {sorted(offered)}"
    assert "m" in offered
    assert "d" in offered, "the detail key was lost"
    assert {"escape", "ctrl+r", "ctrl+n", "ctrl+s", "ctrl+o", "ctrl+q"}.isdisjoint(
        {key for key, _a, _l in SESSION_ACTION_KEYS} | {"m"}
    )


# The keys answer for themselves, and say they exist ---------------------------------------


async def test_the_action_keys_decline_when_no_row_is_highlighted() -> None:
    """`check_action` mirrors the early return the action already has, per this surface's rule
    that a key is refused only where the action it names already declines.

    `highlighted_session()` returns None on an empty list, so every action key is a no-op
    there -- and `check_action` says so rather than leaving Textual to dispatch into a method
    that will silently do nothing.
    """
    app = SessionsPane(_context(()))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.highlighted_session() is None
        assert screen.check_action("row_action", ("force",)) is False
        assert screen.check_action("row_remote_control", ()) is False


async def test_the_action_keys_are_offered_when_a_row_is_highlighted() -> None:
    """The other half: a rule that only ever refuses would hide a working key."""
    app = SessionsPane(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.highlighted_session() is not None
        assert screen.check_action("row_action", ("force",)) is not False
        assert screen.check_action("row_remote_control", ()) is not False


@pytest.mark.parametrize("width", (100, 80, 60))
async def test_the_status_line_names_the_keys_and_is_not_truncated(width: int) -> None:
    """The keys are `show=False`, so the status line is where they are discoverable.

    Not a preference: the footer at the project's own 100-column baseline already runs to
    about seventy columns, and six more entries would clip bindings the owner did not add --
    the defect `InspectScreen`'s own comment records having caused once.

    **Asserted on the rendered line, not on `Static.content`.** The first version of this test
    read the content, which holds the untruncated source string and would have passed at any
    width whatsoever -- while `#status` is `height: 2` and clips. A Tier-1 review reproduced
    the real thing at 60 columns: "m remote control" vanished with no ellipsis at all, which is
    worse than eliding. This is the same pitfall `test_status_region.py`'s own
    `test_the_attach_command_renders_whole_at_eighty_columns` documents, and it is the third
    time in this plan that asserting the input instead of the render hid a real defect.

    60 is included because `app.py`'s own margin comments treat it as a live budget.
    """
    from textual.widgets import Static

    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        status = app.screen.query_one("#status", Static)
        # Rows joined with a space, not concatenated. `#status` is `height: 2` and wraps, and
        # the wrap point falls mid-sentence -- at 60 columns it lands between "a" and
        # "attach", so a bare concatenation reads "aattach" and the assertion fails on a
        # string that is entirely present. Joining with a space can only ever produce a false
        # *failure* (if a wrap split a word), never a false pass, which is the right direction
        # for a test whose whole job is to notice loss.
        drawn = " ".join(status.render_line(row).text.strip() for row in range(status.size.height))

    for key, action, _label in _module_action_keys():
        assert f"{key} {action}" in drawn, f"{key!r} missing at {width} columns: {drawn!r}"
    assert "m remote" in drawn, f"the Remote Control key is missing at {width}: {drawn!r}"
    assert "…" not in drawn, f"the status line was elided at {width} columns: {drawn!r}"


# One key returns the projects surface to the console's left slot -------------------------------


async def test_p_calls_the_wired_capability_exactly_once() -> None:
    """DEC-040: the console exchanges its left pane with the agent, and this puts it back.

    An exchange writes no record and touches no lifecycle, which is why this key needs none of
    the machinery every other key on this pane routes through.
    """
    calls: list[int] = []

    async def show_projects() -> None:
        calls.append(1)

    app = SessionsPane(_context((_record(),), console_show_projects=show_projects))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()

    assert calls == [1], calls


async def test_p_is_absent_on_a_host_that_wired_no_console() -> None:
    """A bare terminal has no console to put a surface back into, so the key is not offered
    and its action declines. A dead-end entry is worse than an absent one -- the owner cannot
    tell a key that does nothing from a surface that forgot to draw it."""
    app = SessionsPane(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.check_action("show_projects_pane", ()) is False
        await pilot.press("p")
        await pilot.pause()
        assert app.is_running


async def test_a_failing_capability_is_reported_and_is_not_a_lifecycle_failure() -> None:
    """The console degrading is not the session going wrong. A raise here must reach the owner
    as what it is -- the surface could not be rearranged -- and must not take the app down or
    read as anything having happened to the agent."""
    from tui_feedback import announcements

    async def show_projects() -> None:
        raise RuntimeError("the console has no left slot")

    app = SessionsPane(_context((_record(),), console_show_projects=show_projects))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        said = " ".join(announcements(app))
        assert app.is_running, "a failing console capability took the surface down"

    assert "console" in said.lower(), said
    assert "session" not in said.lower(), f"a console failure was reported as a session one: {said}"


async def test_the_full_sessions_position_does_not_offer_the_console_key() -> None:
    """Driven, not just read off the class: a `SessionsScreen` with `console_show_projects`
    wired -- the exact combination a `remote-agents tui` on the console's own tmux server
    produces -- must still not carry `p`."""
    calls: list[int] = []

    async def show_projects() -> None:  # pragma: no cover - must never be reached
        calls.append(1)

    app = RemoteAgentsTui(_context((_record(),), console_show_projects=show_projects))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.show_sessions()
        await pilot.pause()
        assert position(app) == "SESSIONS"
        offered = set(app.screen.active_bindings)
        assert "p" not in offered, sorted(offered)
        await pilot.press("p")
        await pilot.pause()

    assert calls == [], "the full sessions position rearranged the console"


def test_a_key_for_an_unconfirmed_action_fails_at_import_not_only_in_ci() -> None:
    """The invariant is enforced where it cannot be skipped.

    It was asserted only by a test, and this codebase already has the better pattern for the
    shape -- `ChoiceScreen.__init_subclass__` raises at class-definition time. Re-adding `s`
    to "finish the job the plan proposed" would otherwise ship the moment someone did not
    notice one red test, putting an unconfirmed graceful stop one keypress from a list whose
    cursor moves under a 10-second refresh.
    """
    import importlib

    import remote_agents.adapters.tui.screens.sessions as module

    original = module.SESSION_ACTION_KEYS
    source = pathlib.Path(module.__file__).read_text("utf-8")
    assert "raise RuntimeError(" in source, "the import-time guard is gone"
    assert "_bindable & UNCONFIRMED_MUTATING_ACTIONS" in source, "the guard no longer checks it"
    # The guard reads the module-level table, so the check is exactly the one that runs at
    # import; reproduced here rather than re-importing a mutated module, which pytest's own
    # module cache makes unreliable.
    bindable = {action for _key, action, _label in original}
    assert not bindable & module.UNCONFIRMED_MUTATING_ACTIONS
    assert module.UNCONFIRMED_MUTATING_ACTIONS, "the rule must not be vacuous"
    importlib.reload  # noqa: B018 - referenced so the import is not flagged unused


async def test_i_is_absent_on_a_host_that_cannot_inspect() -> None:
    """The same rule `p` follows: offered where the capability exists, absent where it does
    not. `detail_entries` gates the Inspect row on `backend.capture` and `show_inspect`
    returns silently without it, so a key that stayed offered would push a detail with no
    Inspect row and then do nothing -- the dead end `p`'s own gating exists to avoid."""
    app = SessionsPane(_context((_record(),), capture=None))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.check_action("row_action", ("inspect",)) is False
        assert screen.check_action("row_action", ("attach",)) is not False
