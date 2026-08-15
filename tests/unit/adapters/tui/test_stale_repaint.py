"""A store read left in flight by a navigation, and what it repaints when it finally lands.

BL-016's claim: navigating away while a worker read is in flight lets the resolver repaint a
screen the owner no longer owns, and can clobber typed input. It was deferred on the argument
that the Textual screen rewrite would fix it structurally. That rewrite has shipped, nobody
had checked, and this file is what decides it. `ChoiceScreen.showing` is the guard under test
— its own docstring names this hazard and records that a sweep found *eleven* methods that
await and then render or push, which is why the guard sits on the shared path rather than at
each call site.

**The answer when this file was written was: half fixed.** Navigating to another position was
already covered — `showing` refuses the stale render, and a value typed on the position the
owner moved to survives untouched. Navigating away *and back* was not: `showing` asks "is this
screen the one on top", which is not the same question as "is this the visit that started the
read". The sessions list is the one screen this is reachable on, because it is the one that
stays on the stack while a detail is pushed over it, and it is the one with a read that
outlives the detour.

**Task 2.1 closed that half**, and the last test in this file — which was
`xfail(strict=True)` when it was the reproduction — is now the regression net for the fix.
`SessionsScreen` carries a per-visit counter (`_visit`), bumped in **both** `on_reveal` and
`on_screen_resume` and captured by `_auto_reload` across its await, so a read issued during an
earlier visit is dropped rather than drawn. Deliberately *not* a redefinition of `showing`:
the eleven callers its docstring names want the question it already answers, and conflating
the two would have changed every render guard in the package to fix one screen.

The two bump sites are not redundant. `on_screen_resume` arrives as a message on this screen's
own pump task; `on_reveal` is awaited by `go_back` on the app's task, at the instant of the
pop. Bumping only on the message left the outcome to whichever task the scheduler resumed
first — see the last test, which measures that ordering and is explicit that it cannot pin it.

**Why none of this is driven by holding Ctrl+R down and pressing escape, which is the shape
the defect is written in.** Every awaited read in this surface is awaited *inside a message
pump*, and Textual serialises each pump. `App.action_refresh` runs on the app's own pump, and
every key event enters through `App.on_event` on that same pump — so for the whole duration of
a keyed refresh the surface accepts no input at all. A read awaited in a *screen's* handler
(`populate`, `choose`) is no better: the key is forwarded to the focused widget and has to
bubble back up through that blocked screen to reach the app's bindings. Measured both ways,
and the first test in this file pins it: escape pressed during a Ctrl+R is delivered, does
nothing, and takes effect only once the read has resolved. So the keyed refresh cannot be
navigated out from under, and a test that claimed to do it would be describing a sequence the
framework cannot produce.

The one read that leaves the keyboard live is the sessions list's own interval
(`SessionsScreen._auto_reload`): a `Timer` invokes its callback in the timer's task rather
than on a pump, so nothing is blocked while it waits on the store. That is the read these
tests leave behind, and it is not a contrivance — it is the read most likely to be in flight
when an owner navigates, because it is the only one they did not ask for.

`_auto_reload` is called directly rather than waited for, because `_SESSIONS_AUTO_REFRESH` is
ten seconds. The call is what `Timer._tick` does with it (`await` the callback, in its own
task), so nothing about the delivery is faked; only the wait is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from test_tui_snapshots import settle
from textual.widgets import Input, OptionList
from tui_feedback import status
from tui_filter import settle_filter
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import SessionsScreen
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_ALPHA = CatalogProject("opaque-alpha", "alpha", "infra", "Registered")
_BETA = CatalogProject("opaque-beta", "beta", "infra", "Registered")
_ONE = SessionId.new()
_TWO = SessionId.new()


def _record(session_id: SessionId, ordinal: int) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-alpha"),
        ProfileId("claude"),
        SessionDisplayIdentity("alpha", "claude", "regular", ordinal),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _GatedLauncher:
    """A store whose next listing can be held open, answering the world as it was.

    Two properties, and both are the test rather than the scaffolding.

    `gate` is armed for exactly one call and cleared as that call takes it, so the read the
    test wants to leave in flight is the only one held — every read the *navigation* itself
    performs (opening a detail, coming back to the list, a keyed refresh) has to run to
    completion or the owner could not navigate at all.

    The answer is snapshotted at call time rather than read at return time, because that is
    what makes the stale read stale: a listing that began before the owner left describes the
    host as it was then, and the whole question is what happens when that description lands on
    a screen that has since been redrawn from a later one.

    `started` is an `asyncio.Event` rather than a sleep for the reason `_SlowLauncher` in
    `test_tui_worker_exclusivity.py` records: a window opened with a timer holds on an idle
    machine and fails under load, which is the flake these tests exist to be evidence against.
    """

    records: tuple[SessionRecord, ...]
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        answer = self.records
        gate, self.gate = self.gate, None
        if gate is not None:
            self.started.set()
            await gate.wait()
        return answer

    async def copy_attach(self, _session_id: SessionId) -> str | None:
        return None


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")


def _context(launcher: _GatedLauncher) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_ALPHA, _BETA),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_ALPHA, _BETA),
    )


def _rows(app: RemoteAgentsTui) -> list[str | None]:
    """The row keys the position on screen is currently offering."""
    choices = app.screen.query_one("#choices", OptionList)
    return [choices.get_option_at_index(index).id for index in range(choices.option_count)]


def _entry(app: RemoteAgentsTui) -> Input:
    return app.screen.query_one("#filter", Input)


def _surface(app: RemoteAgentsTui) -> tuple[str, str, str, bool, list[str | None]]:
    """Everything the owner can see of the position on screen, in one comparable value.

    Compared as a whole rather than field by field so that a stale render is caught wherever
    it lands. The typed value and where the keyboard is are in here for BL-016's second half:
    the rows could be left alone and the entry still cleared, or the keyboard pulled off it
    onto a list, and either would be the defect this file is about.
    """
    entry = _entry(app)
    return (position(app), status(app), entry.value, entry.has_focus, _rows(app))


async def _a_listing_read_left_in_flight(
    app: RemoteAgentsTui, pilot, launcher: _GatedLauncher, gate: asyncio.Event
) -> tuple[SessionsScreen, asyncio.Task[None]]:
    """Put the sessions list on screen and leave one of its own store reads unresolved.

    The interval's callback rather than Ctrl+R, for the reason the module docstring gives at
    length: a keyed refresh is awaited on the app's pump and freezes every key for its
    duration, so there is no navigation to observe. This is the same read against the same
    store, started from the one place in this surface that does not hold a pump while it
    waits.

    `gate` is passed in rather than made here so the caller's `finally` can always open it. A
    gate this function owned would be unreachable if this function failed after arming it, and
    the held read would then keep a message pump alive through the app's own teardown — a
    hang instead of a failure, which is the worst way for an assertion to go wrong.
    """
    await pilot.press("ctrl+s")
    await settle(app, pilot)
    screen = app.screen
    assert isinstance(screen, SessionsScreen), f"expected the sessions list, got {screen!r}"
    launcher.gate = gate
    reading = asyncio.create_task(screen._auto_reload())
    await asyncio.wait_for(launcher.started.wait(), timeout=5)
    return screen, reading


async def _settled(reading: asyncio.Task[None] | None) -> None:
    """Let a released read finish before the test returns, whatever happened above.

    Recorded as a Suggestion by the Stage 1 gate's Tier-2 pass and fixed here, while Task 2.1
    had the file open. The read is awaited on each test's happy path only, so an assertion
    raising between arming the gate and that await left the task unawaited: the `finally`
    releases it, but nothing collects it, and asyncio then reports "Task was destroyed but it
    is pending" during teardown. That noise lands on exactly the runs where something already
    failed, on top of the real assertion error — which is the worst moment to make output
    harder to read.

    Exceptions are suppressed rather than raised: this runs in a `finally`, and a read that
    failed because the surface was already being torn down must not replace the assertion
    error that explains why.
    """
    if reading is None:
        return
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(reading, timeout=5)


async def test_a_keyed_refresh_holds_the_pump_so_nothing_can_navigate_out_from_under_it() -> None:
    """The premise the rest of this file rests on, driven rather than assumed.

    BL-016 is written as "press Ctrl+R, then navigate away before it resolves". This is that
    sequence, with both halves delivered as real keys, and it shows the second half never
    happens: `action_refresh` is awaited inside the app's message pump, and `App.on_event` —
    where every key enters — runs on that same pump, so the escape sits in the queue until the
    read is done. The position does not move while the read is in flight.

    The last two assertions are what keep this from being vacuous in the way it most easily
    could be. The escape is not silently dropped by the test harness: once the read resolves
    it is processed and the surface leaves the sessions list, which is only possible if the
    key was genuinely delivered while the surface was ignoring it. And the refresh really was
    unfinished at the moment the escape was pressed, rather than having quietly completed.

    Kept in this file rather than left as a note, because without it the three tests below
    look like they chose an obscure read out of preference. They did not: it is the only read
    in this surface a navigation can overlap at all. The one other arrangement that leaves the
    keyboard live — a read awaited behind an open confirmation modal, where focus has moved off
    the blocked screen — is a read taken under `holding_the_guard`, and `action_back` and all
    three flow jumps return early while that is held. So the keyboard is live and every key
    that could navigate is refused, which is the same outcome by the other route.
    """
    launcher = _GatedLauncher(records=(_record(_ONE, 1), _record(_TWO, 2)))
    app = RemoteAgentsTui(_context(launcher))
    gate = asyncio.Event()
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+s")
            await settle(app, pilot)
            assert position(app) == "SESSIONS"

            launcher.gate = gate
            # Not awaited: `Pilot.press` waits for the screen to finish processing, and the
            # whole point of this test is that it never does until the gate opens.
            refreshing = asyncio.create_task(pilot.press("ctrl+r"))
            await asyncio.wait_for(launcher.started.wait(), timeout=5)

            leaving = asyncio.create_task(pilot.press("escape"))
            # Long enough for the key to reach the driver and be queued; the assertion is that
            # queuing is all that happens to it.
            await asyncio.sleep(0.2)
            assert not refreshing.done(), (
                "the refresh finished before the escape was pressed, so this test says nothing "
                "about what a keypress can do while a read is in flight"
            )
            assert position(app) == "SESSIONS", (
                "the surface navigated during a keyed refresh; if that is now possible the "
                "three tests below are testing the wrong read"
            )

            gate.set()
            await asyncio.wait_for(asyncio.gather(refreshing, leaving), timeout=10)
            await settle(app, pilot)
            assert position(app) == "PROJECTS", (
                "the escape never took effect at all, so the assertion above passed because "
                "the key was dropped rather than because the surface was holding its pump"
            )
    finally:
        # No `_settled` here: this test never leaves a read in flight — it holds the *pump*,
        # not a task — so there is nothing to collect and implying otherwise would send a
        # reader looking for a leak that does not exist.
        gate.set()


async def test_a_read_left_behind_does_not_clobber_a_name_typed_on_the_position_moved_to() -> None:
    """Navigate out of the sessions list mid-read, into the add-project flow, and type.

    The first half of BL-016: the resolver must not repaint a screen the owner no longer owns.
    `NameScreen` is the position it would be worst on — `entry_is_a_commitment` is true there,
    so what is in the box is the payload of the step, not a filter that costs one keystroke to
    retype — and the sessions list's own `_draw_listing` would write the status line and the
    rows out from under it.

    Three things are asserted positively so that this cannot pass by not happening: the read
    had genuinely started, it was still unresolved at the moment the whole surface state was
    captured, and the position had actually changed. Without those, a resolver that had
    already finished would produce the same green.
    """
    launcher = _GatedLauncher(records=(_record(_ONE, 1), _record(_TWO, 2)))
    app = RemoteAgentsTui(_context(launcher))
    gate = asyncio.Event()
    reading: asyncio.Task[None] | None = None
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen, reading = await _a_listing_read_left_in_flight(app, pilot, launcher, gate)
            assert position(app) == "SESSIONS"

            await pilot.press("ctrl+n")
            await settle(app, pilot)
            assert position(app) == "AREAS"
            await pilot.press("enter")
            await settle(app, pilot)
            assert position(app) == "NAME", "the add-project flow did not reach the name entry"
            await pilot.press(*"orbit-relay")
            await pilot.pause()
            assert _entry(app).value == "orbit-relay", "the name never reached the entry"

            before = _surface(app)
            assert not reading.done(), (
                "the listing read resolved before the navigation, so nothing stale is left to "
                "land and this test proves nothing"
            )
            assert screen is not app.screen, "the owner never left the sessions list"

            gate.set()
            await asyncio.wait_for(reading, timeout=5)
            await pilot.pause()
            assert _surface(app) == before, (
                f"a listing read the owner walked away from repainted the position they moved "
                f"to: {before} became {_surface(app)}"
            )
    finally:
        gate.set()
        await _settled(reading)


async def test_a_read_left_behind_does_not_clobber_a_label_typed_two_flows_away() -> None:
    """The same guarantee across a longer walk, ending on the launch wizard's label.

    A second destination rather than a second assertion on the first, because the two reach
    `LabelScreen` and `NameScreen` by different routes: this one leaves the sessions list by
    *popping* it (escape, through `go_back`, which re-reads the project list on the way) and
    then walks three positions deeper, while the other jumps flows with `switch_flow`. The
    stale read is left behind by both, and a guard that covered one arrangement of the stack
    and not the other would be caught here.

    The filter is typed and settled deliberately: it is what makes the project row the owner
    selects the one they were looking at, and `settle_filter` exists because the debounce
    otherwise decides that by timing.
    """
    launcher = _GatedLauncher(records=(_record(_ONE, 1), _record(_TWO, 2)))
    app = RemoteAgentsTui(_context(launcher))
    gate = asyncio.Event()
    reading: asyncio.Task[None] | None = None
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen, reading = await _a_listing_read_left_in_flight(app, pilot, launcher, gate)

            await pilot.press("escape")
            await settle(app, pilot)
            assert position(app) == "PROJECTS"
            await pilot.press(*"alpha")
            await settle_filter(pilot)
            assert _rows(app) == ["opaque-alpha"], "the filter did not narrow to one project"
            await pilot.press("down", "enter")
            await settle(app, pilot)
            assert position(app) == "PROFILES"
            await pilot.press("enter")
            await settle(app, pilot)
            assert position(app) == "LABEL", "the launch wizard did not reach the label entry"
            await pilot.press(*"nightly-sweep")
            await pilot.pause()
            assert _entry(app).value == "nightly-sweep", "the label never reached the entry"

            before = _surface(app)
            assert not reading.done(), (
                "the listing read resolved before the navigation, so nothing stale is left to "
                "land and this test proves nothing"
            )
            assert screen is not app.screen, "the owner never left the sessions list"

            gate.set()
            await asyncio.wait_for(reading, timeout=5)
            await pilot.pause()
            assert _surface(app) == before, (
                f"a listing read the owner walked away from repainted the position they moved "
                f"to: {before} became {_surface(app)}"
            )
    finally:
        gate.set()
        await _settled(reading)


async def test_a_read_left_behind_does_not_repaint_the_position_the_owner_came_back_to() -> None:
    """BL-016, still live: `showing` cannot tell this visit from the one that read the store.

    The owner is on the sessions list with two sessions on it. The interval's read is in
    flight when they open one session's detail; while they are in there the second session
    ends — the store has a second writer, which is the whole reason this list re-reads itself
    at all. They come back, and press Ctrl+R to be sure, and the list correctly shows one
    session. Then the read they left behind lands and puts the ended session back on the
    screen, over the answer the owner explicitly asked for.

    `showing` does not catch it and cannot: it compares `app.screen is self`, and by the time
    this resolver resumes that is true again. It is the right question for the case the guard
    was written for — a screen *popped* mid-read never comes back, so the identity stays false
    — and the wrong one here, because the sessions list is the position that stays on the
    stack underneath the detail pushed over it. Every render this resolver reaches is guarded,
    and every guard says yes.

    What that leaves on screen is a row offering `Stop` for a session that has already ended,
    which the re-read inside `stop` would refuse — DEC-007's fourth mitigation, doing the job
    it exists for, over rows that should never have been redrawn.

    **Task 2.1 owns the fix and closes this by deleting the marker.** What it needs is a
    per-visit identity that a resolver can carry and check on the way out, in the shape
    `_resting_generation` already has for the deferred cursor placement: a counter bumped when
    the position is re-entered, captured by the read, and compared before the draw. `showing`
    is not it and should not be made into it — the two questions are different, and the
    eleven callers named in its docstring want the one it already answers.
    """
    launcher = _GatedLauncher(records=(_record(_ONE, 1), _record(_TWO, 2)))
    app = RemoteAgentsTui(_context(launcher))
    gate = asyncio.Event()
    reading: asyncio.Task[None] | None = None
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen, reading = await _a_listing_read_left_in_flight(app, pilot, launcher, gate)
            assert _rows(app) == [str(_ONE), str(_TWO)]

            # The other writer ends the second session while the owner is in the detail. The
            # in-flight read already has its answer and knows nothing about this.
            launcher.records = (_record(_ONE, 1),)

            await pilot.press("enter")
            await settle(app, pilot)
            assert position(app) == "SESSION_DETAIL", "the detail never opened"
            assert not reading.done(), (
                "the listing read resolved before the owner navigated, so there is nothing "
                "stale left to land"
            )

            await pilot.press("escape")
            await settle(app, pilot)
            assert position(app) == "SESSIONS", "the owner did not come back to the list"
            await pilot.press("ctrl+r")
            await settle(app, pilot)
            assert _rows(app) == [str(_ONE)], (
                "the refresh did not pick up the ended session, so the assertion below would "
                "hold for a surface with no stale read at all"
            )
            assert not reading.done(), "the read under test finished before it was released"

            gate.set()
            await asyncio.wait_for(reading, timeout=5)
            await pilot.pause()
            assert _rows(app) == [str(_ONE)], (
                "a listing read from before the owner left repainted the list they came back "
                "to: the ended session is on screen again, offering actions against a pane "
                "that is gone"
            )
    finally:
        gate.set()
        await _settled(reading)


async def test_a_read_landing_in_the_gap_between_the_pop_and_the_resume_is_still_dropped() -> None:
    """The narrow half of the same defect, which the test above cannot reach.

    Added at Task 2.1's Tier-1 review, which found the first fix incomplete. The bump was
    originally made only in `on_screen_resume` — and that runs on *this screen's* message-pump
    task, whenever it next drains, because `ScreenResume` is a message. `go_back` meanwhile
    calls `pop_screen()` and awaits `on_reveal()` directly on the *app's* task. `go_back`'s own
    docstring already recorded the ordering: "Textual's own `ScreenResume` would run after the
    pop returned, which is outside that guard."

    So a stale read resolving in the window between the pop and that message being drained
    would still find `visiting == self._visit` and draw — the same defect with a narrower
    window rather than a closed one. The test above cannot see it: it holds the read open
    until after a full `settle()` and a keyed refresh, which is long past the point
    `on_screen_resume` is guaranteed to have run.

    This releases the stale read immediately after `go_back()` returns — no `settle`, no
    `pause`, no keyed refresh — which is a far tighter window than the test above holds open.

    **What it does not do is discriminate between the two bump sites, and saying so is the
    point.** Measured ordering, with the bump instrumented:

        go_back returned -> on_screen_resume -> read landed

    `on_screen_resume` really does run after `go_back` returns, exactly as the review said.
    But releasing the gate cannot complete the read without yielding, and the same yield lets
    this screen's pump task drain `ScreenResume` — so the bump still wins, and removing the
    `on_reveal` bump leaves this test green. Verified by mutation rather than assumed.

    Which leaves the race real but **scheduler-ordered**: whether the read's task or the
    pump's task is resumed first after the gate opens is not something the framework promises,
    and a test cannot pin an ordering the scheduler is free to pick either way. The
    `on_reveal` bump is what makes the answer independent of that choice, which is why it is
    there and why this test cannot be the evidence for it. What this test *is* evidence for is
    the tighter window itself: a read released the instant the pop returns is dropped.
    """
    launcher = _GatedLauncher(records=(_record(_ONE, 1), _record(_TWO, 2)))
    app = RemoteAgentsTui(_context(launcher))
    gate = asyncio.Event()
    reading: asyncio.Task[None] | None = None
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen, reading = await _a_listing_read_left_in_flight(app, pilot, launcher, gate)
            assert _rows(app) == [str(_ONE), str(_TWO)]

            launcher.records = (_record(_ONE, 1),)
            await pilot.press("enter")
            await settle(app, pilot)
            assert position(app) == "SESSION_DETAIL", "the detail never opened"
            assert not reading.done(), "the read resolved before the navigation"

            # `go_back` rather than an escape keypress, because a keypress would be delivered
            # through the pump and drain `ScreenResume` on the way — closing by accident the
            # very window this test exists to hold open.
            await app.go_back()
            assert position(app) == "SESSIONS", "the pop did not reveal the sessions list"
            assert not reading.done(), "the read under test finished before it was released"

            # Released here, in the gap: `on_reveal` has run (go_back awaited it) but nothing
            # has yet given this screen's own pump a turn to deliver `ScreenResume`.
            gate.set()
            await asyncio.wait_for(reading, timeout=5)
            assert _rows(app) == [str(_ONE)], (
                "a read from the previous visit landed in the gap between the pop and the "
                "ScreenResume message, and was drawn: the ended session is on screen again"
            )
    finally:
        gate.set()
        await _settled(reading)
