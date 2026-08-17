"""Naming a session happens on the session, from the surface that is holding it.

The bot has been able to rename a running session since its detail screen grew the row; the
local surface could only ever name one *at launch*, which is the wrong moment — the name is
chosen before there is anything to look at, and it can never be changed afterwards. DEC-007
says the local terminal is a full control plane rather than a launch wizard, and rename was
the one post-launch capability still missing from it.

What these tests pin is the pair that makes the affordance honest rather than just present:
the row reaches `SessionService.rename` with the *normalized* label, and every way of not
naming something — an over-long entry, an empty one, a session that ended while the box was
open — issues no call at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from textual.widgets import Input, OptionList
from tui_feedback import announcements, breadcrumb
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import InspectScreen
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(label: str | None = None, state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1, custom_label=label),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing:
    records: tuple[SessionRecord, ...] = ()
    #: Every `(session_id, label)` this surface asked for, in order. The label is what the
    #: assertions are actually about: the screen must hand over the *normalized* value, not
    #: the raw keystrokes.
    renamed: list[tuple[SessionId, str | None]] = field(default_factory=list)
    #: When set, `rename` records its arrival and then parks until the event is released. The
    #: only way to hold a submit open long enough for a second one to start, which is the
    #: window the concurrent test below exists to reach. `slots=True` rules out swapping the
    #: method on the instance, so the seam lives here.
    gate: asyncio.Event | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def rename(self, session_id: SessionId, label: str | None) -> SessionRecord:
        """The store's own behaviour: the label lands on the record and the read reflects it.

        Written out rather than returning the unchanged record, because the test that matters
        most here asserts the surface *shows* the new name — and it would pass against a fake
        that renamed nothing if the surface simply redrew the old one.
        """
        self.renamed.append((session_id, label))
        if self.gate is not None:
            await self.gate.wait()
        current = next(item for item in self.records if item.session_id == session_id)
        renamed = replace(current, display=replace(current.display, custom_label=label))
        self.records = tuple(
            renamed if item.session_id == session_id else item for item in self.records
        )
        return renamed


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_EXISTING,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_EXISTING,),
        max_label_length=8,
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


async def test_the_detail_offers_a_rename_row() -> None:
    """The affordance exists at all, spelled the same word the bot spells it."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        rows = _rows(app)

    assert "Rename" in rows, f"the detail offered {rows}"


async def test_choosing_rename_opens_an_entry_of_its_own() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        step = position(app)

    assert step == "RENAME"


async def test_submitting_a_name_renames_the_session_and_comes_back_naming_it() -> None:
    """The whole affordance, end to end: the call is made and the surface shows the result.

    Both halves are asserted because either alone passes while the feature is half-built — a
    call with no redraw leaves the owner looking at the old name, and a redraw with no call
    renames nothing.
    """
    record = _record()
    launcher = _Listing((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        app.screen.query_one(Input).value = "nightly"
        await app.screen.submit("nightly")
        await pilot.pause()
        step = position(app)
        trail = breadcrumb(app)

    assert launcher.renamed == [(record.session_id, "nightly")]
    assert step == "SESSION_DETAIL", "renaming must return to the session it renamed"
    assert "nightly" in trail, f"the detail came back naming {trail!r}"


async def test_a_name_over_the_bound_is_refused_while_it_is_typed() -> None:
    """Said at the keystroke that broke it, in the words the shared rule already wrote.

    `max_label_length=8` in the fixture, so nine characters is one too many. The bound is the
    host's configured one rather than the domain ceiling, which is why the fixture sets it.
    """
    record = _record()
    launcher = _Listing((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        entry = app.screen.query_one(Input)
        entry.value = "far-too-long"
        await pilot.pause()
        warned = announcements(app, severity="warning")

    assert warned, "an over-long name was accepted silently while it was typed"
    assert launcher.renamed == [], "nothing may be renamed by typing"


async def test_an_over_long_name_submitted_anyway_issues_no_call() -> None:
    """The typed-time warning tells the owner sooner; it never becomes the gate."""
    record = _record()
    launcher = _Listing((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        await app.screen.submit("far-too-long")
        await pilot.pause()
        step = position(app)

    assert launcher.renamed == []
    assert step == "RENAME", "a refused name must leave the owner on the entry, not advance"


async def test_an_empty_entry_leaves_the_name_as_it_is() -> None:
    """Declining to rename is not the same act as clearing a name.

    The store supports `set_label(None)` and neither surface offers it: the bot's Skip
    deliberately does not clear (`telegram/service.py:485-488`), so this one does not either.
    A surface where the only way to lose a name is an empty box would lose one by accident.
    """
    record = _record(label="keep-me")
    launcher = _Listing((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        await app.screen.submit("")
        await pilot.pause()
        step = position(app)

    assert launcher.renamed == [], "an empty entry must not clear the name"
    assert step == "SESSION_DETAIL", "declining to rename returns to the session"


async def test_a_repeated_enter_renames_once_and_does_not_pop_twice() -> None:
    """Two Enters on the entry are one rename, and they leave the owner on the detail.

    **This is the only mutating `Input.Submitted` handler in the surface, and it was written
    without the guard its two siblings have.** `LabelScreen.submit` and `NameScreen.submit`
    both check `showing` before acting, so a second Enter arriving after the first has left the
    screen returns early. This one did not, and its docstring credited the busy guard for
    refusing the repeat — which that guard cannot do here: `tui.busy` is read by
    `check_action` and by `on_option_list_option_selected`, and nothing on the `Input` dispatch
    path consults it. `awaiting()` does not cover the entry either, only `#choices`.

    The second assertion is the one that bites. A duplicate rename writes the same label twice
    and is invisible; a duplicate `go_back()` pops a second screen, so a doubled keystroke
    silently lands the owner on the sessions list instead of the session they just named.
    """
    record = _record()
    launcher = _Listing((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        screen = app.screen
        await screen.choose("rename")
        await pilot.pause()
        entry = app.screen.query_one(Input)

        # Two submits on the same screen object, which is what the screen's own message pump
        # delivers when the owner presses enter twice: the handlers run in order, and the
        # second one runs on a screen the first has already left.
        renamer = app.screen
        entry.value = "nightly"
        await renamer.submit("nightly")
        await pilot.pause()
        await renamer.submit("nightly")
        await pilot.pause()
        step = position(app)

    assert launcher.renamed == [(record.session_id, "nightly")], (
        f"a repeated enter issued {len(launcher.renamed)} renames"
    )
    assert step == "SESSION_DETAIL", f"a repeated enter left the owner on {step}"


async def test_a_second_enter_arriving_mid_rename_is_dropped_too() -> None:
    """The other window the sequential test cannot reach: a repeat while the first is suspended.

    The sequential case above is what the screen's own pump delivers. This one forces the case
    it cannot produce — two `submit` calls genuinely in flight at once — by gating the fake's
    rename on an event, so the first is parked inside its guarded block when the second starts.
    Written because a review named this window specifically and neither of us could settle it
    by reading: `holding_the_guard` sets `tui.busy`, but nothing on the `Input` path consults
    it, so if this window is reachable the `showing` check is not enough on its own.
    """
    record = _record()
    released = asyncio.Event()
    launcher = _Listing((record,), gate=released)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        renamer = app.screen

        first = asyncio.create_task(renamer.submit("nightly"))
        # Let the first reach the gated rename and park there.
        for _ in range(5):
            await pilot.pause()
        assert len(launcher.renamed) == 1, (
            f"the first submit did not park in the store: {launcher.renamed}"
        )

        second = asyncio.create_task(renamer.submit("nightly"))
        for _ in range(5):
            await pilot.pause()
        # Recorded before releasing, so the assertion is about what happened *during* the
        # window rather than after it.
        during = list(launcher.renamed)

        released.set()
        await asyncio.gather(first, second)
        await pilot.pause()
        step = position(app)

    assert during == [(record.session_id, "nightly")], (
        f"a second enter reached the store while the first was in flight: {during}"
    )
    assert step == "SESSION_DETAIL", f"a concurrent repeat left the owner on {step}"


async def test_a_rename_landing_after_the_owner_left_does_not_pop_their_position() -> None:
    """The post-await `showing` check, which the two repeat tests do not reach.

    A re-review mutation-tested all three guards and found this one unpinned: deleting it left
    every other test in this file green, and `test_teardown_during_flight.py` green too. Both
    repeat tests intercept the second submit at the *entry* check, so neither ever runs the line
    after the store call.

    What reaches it is not a repeat at all — it is one submit whose write lands after the owner
    has been taken somewhere else. `go_back` pops whatever is on top and has no liveness check
    of its own (`app.py`'s `go_back` is an unconditional pop past the stack-depth test), so
    without the guard a finished rename would pop the position the owner is now looking at.
    """
    record = _record()
    released = asyncio.Event()
    launcher = _Listing((record,), gate=released)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        renamer = app.screen

        parked = asyncio.create_task(renamer.submit("nightly"))
        for _ in range(5):
            await pilot.pause()
        assert len(launcher.renamed) == 1, "the submit did not park in the store"

        # The owner is somewhere else by the time the write lands. Pushed directly rather than
        # navigated: every ordinary way out is refused while the guard is held, which is the
        # point — this is the residual window `advance_to`'s docstring describes, where a
        # priority binding reaches the App's own pump that a suspended screen handler does not
        # hold.
        await app.push_screen(InspectScreen("elsewhere"))
        await pilot.pause()
        assert position(app) == "INSPECT", "the fixture never left the rename entry"

        released.set()
        await parked
        await pilot.pause()
        step = position(app)

    # The rename still landed — leaving is not cancelling.
    assert launcher.renamed == [(record.session_id, "nightly")]
    assert step == "INSPECT", f"the finished rename popped the owner to {step}"


async def test_a_session_that_ended_while_the_box_was_open_lands_on_the_list() -> None:
    """The store has a second writer, so the session can go while the entry is on screen.

    Its detail is gone too, so the list is the only honest place to land — the same answer the
    bot's rename gives for the same reason.
    """
    record = _record()
    launcher = _Listing((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("rename")
        await pilot.pause()
        launcher.records = ()
        await app.screen.submit("nightly")
        await pilot.pause()
        step = position(app)

    assert launcher.renamed == [], "a session that has gone must not be renamed"
    assert step == "SESSIONS", f"landed on {step} rather than the list"
