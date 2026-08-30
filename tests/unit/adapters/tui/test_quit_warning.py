"""Quit with unsaved work: a warning that asks once, never a refusal that traps the owner.

BL-025's claim: `ctrl+q` is a global key that silently discards typed input. Reproduced when
it was opened by typing a label, pressing it, and watching the app exit with the label gone.
`screens/base.py` said so outright — quit is deliberately absent from the set of keys greyed
while `work_in_flight`, because the two kinds of key mean different things to the person
pressing them. A flow jump means "go elsewhere in this app", and losing the work is a side
effect nobody asked for; quit means "leave", and an app that refuses to close until an entry
is cleared is a worse answer than the one it replaces.

So the entry's own `Next step` names the shape: *a warning rather than a refusal — say what is
about to be lost and ask once*. `work_in_flight` is the predicate, and the split status region
and `notify` built by the modernization's sub-plan 3 are the somewhere to put the question.

**Why the question is not a modal, which is what a reader expects here.** The obvious
implementation is `ask_to_confirm`, the way the force stop and Remote Control confirmations
work. **DEC-025 forbids it in exactly this position.** That decision — recorded after this
sub-plan was authored — says a confirmation may only ever be asked from a screen's own
handler, and names "a global binding" among the callers it exists to warn off. `action_quit`
is a global binding. The hazard is that `ask_to_confirm` suspends on a modal that nothing
guarantees will be answered: pop it for any reason and the await is never satisfied and never
fails. The protection every existing confirmation enjoys is that a screen handler holds the
pump while it waits, and a global binding does not.

The shape adopted instead is **arm-then-confirm on the key itself**: the first `ctrl+q` over
work in flight announces what would be lost and arms; the second leaves. Nothing suspends,
nothing can hang, and the owner can always get out — which is the failure mode BL-025 says is
worse than the one being fixed, and which `test_quit_always_leaves_on_the_second_press` pins.

**The arming is about interaction, not about text, and the first version got that wrong.** It
records the position and the text at risk when it warned — but *any key other than another
`ctrl+q` disarms it*, in one `on_event` override rather than a disarm in each of the five
actions.

The value comparison alone was a Critical, reproduced by a Tier-1 review and pinned by
`test_quit_warns_again_after_the_work_was_cleared_and_retyped_identically`: nothing cleared
the arm, so typing `orbit-relay`, being warned, clearing the entry and retyping the same name
quit on a single press — the signature matched an arm held over work that no longer existed.
Correcting a typo back to its original value is an ordinary thing to do, and it discarded the
work exactly as the defect this file exists to close did. A value can express "different
work"; it cannot express "this same continuous stretch of typing", which is what the guarantee
actually needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from backends import backend_for
from test_tui_snapshots import settle
from textual import events
from tui_feedback import announcements, status
from tui_positions import position

from remote_agents.adapters.tui.app import ALL_SCREENS, RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


def _context() -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=object(),  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


async def _typing_a_project_name(app: RemoteAgentsTui, pilot) -> None:
    """Reach the name entry of the create flow and type into it, without committing.

    The create flow's name entry rather than the launch label, because it is the shortest
    route to a screen whose `work_in_flight` is true for the ordinary reason — a shown,
    non-empty entry that is a commitment — and because it is the flow BL-025 was reproduced
    on.
    """
    await app.show_areas()
    await settle(app, pilot)
    await app.screen.choose("infra")
    await pilot.pause()
    await pilot.press(*"orbit-relay")
    await pilot.pause()


async def test_quit_with_typed_work_warns_and_does_not_leave() -> None:
    """The reproduction, inverted into the guarantee: the first press says what is at risk."""
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)
        assert app.screen.work_in_flight, "nothing was in flight, so quit has nothing to warn on"

        await pilot.press("ctrl+q")
        await pilot.pause()

        assert app.is_running, (
            "ctrl+q left with a half-typed name, which is the defect BL-025 recorded"
        )
        warned = announcements(app, severity="warning")
        assert warned and "orbit-relay" in warned[-1], (
            f"the warning has to name what is about to be lost; the surface said {warned}"
        )


async def test_quit_leaves_the_typed_work_exactly_where_it_was() -> None:
    """Declining is free: the warning must not disturb the position or the text.

    The whole argument for warning rather than refusing is that the owner stays in control. A
    warning that cleared the entry, moved the cursor or unfocused the box would be a second
    surprise on top of the first, and would make the second press land somewhere else.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)
        entry = app.screen.query_one("#filter")
        before = (position(app), status(app), entry.value, entry.has_focus)

        await pilot.press("ctrl+q")
        await pilot.pause()

        # Asserted first: without it this whole test passes on a surface that simply left,
        # since a torn-down screen answers these reads as happily as a live one.
        assert app.is_running, "the press left instead of warning, so there is nothing to check"
        entry = app.screen.query_one("#filter")
        assert (position(app), status(app), entry.value, entry.has_focus) == before, (
            "the quit warning disturbed the position it warned about"
        )


async def test_quit_always_leaves_on_the_second_press() -> None:
    """The refusal shape is explicitly not adopted, and this is the test that says so.

    BL-025 is emphatic that an app which will not close until an entry is cleared is a worse
    answer than the silent discard it replaces. So the second press leaves, with the work
    still unsaved, because that is what the owner asked for twice.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)

        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running, "the first press should have warned rather than left"

        await pilot.press("ctrl+q")
        await pilot.pause()

    assert not app.is_running, (
        "the app could not be quit on a second deliberate press — a refusal, which is the "
        "failure mode BL-025 says is worse than the one it replaces"
    )
    assert app.return_value is None, "quitting is not an attach request"


async def test_quit_does_not_warn_when_nothing_is_in_flight() -> None:
    """No work, no question. A warning on every quit is a warning nobody reads."""
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not app.screen.work_in_flight, "the resting position should hold no work"

        await pilot.press("ctrl+q")
        await pilot.pause()

    assert not app.is_running, "quit asked a question it had no reason to ask"


async def test_quit_warns_again_when_the_typed_work_has_changed_since() -> None:
    """The arming is keyed to the work, so a stale yes cannot carry a later, larger loss.

    Warn on `orbit`, keep typing, and the second press is no longer an answer to the question
    that was asked — the thing at risk is not what the owner was told about. A bare flag would
    have left this leaving silently.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_areas()
        await settle(app, pilot)
        await app.screen.choose("infra")
        await pilot.pause()
        await pilot.press(*"orbit")
        await pilot.pause()

        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running, "the first press should have warned"
        first = announcements(app, severity="warning")[-1]

        await pilot.press(*"-relay")
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()

        assert app.is_running, (
            "quit left on a yes that answered a question about different work — the owner was "
            "warned about 'orbit' and lost 'orbit-relay'"
        )
        second = announcements(app, severity="warning")[-1]
        assert "orbit-relay" in second and second != first, (
            f"the second warning should name the work now at risk; got {second!r}"
        )


async def test_quit_warns_again_after_the_work_was_cleared_and_retyped_identically() -> None:
    """The Critical a Tier-1 review reproduced: a stale arm silently re-opened BL-025.

    The first version of this feature compared the arm by *value* — position plus the text at
    risk — and never cleared it. So this sequence quit with no warning at all:

        type `orbit-relay` -> ctrl+q (warned, armed) -> decline
        -> backspace the entry clean -> retype `orbit-relay` -> ctrl+q

    The freshly computed signature equalled a signature armed against work that no longer
    existed, so the second press read as an answer to a question nobody had been asked about
    *this* text. Correcting a typo back to its original value is an ordinary thing to do, and
    it discarded the work exactly as the defect this task closes did.

    The fix is that arming is about *interaction*, not about text: any key that is not another
    `ctrl+q` disarms. A value can never express "this same continuous stretch of typing".
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)

        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running, "the first press should have warned"

        # Cleared and retyped to exactly the same value, which is what made the stale arm
        # match. `work_in_flight` goes false in the middle and comes back.
        for _ in range(len("orbit-relay")):
            await pilot.press("backspace")
        await pilot.pause()
        assert not app.screen.work_in_flight, "the entry was not actually cleared"
        await pilot.press(*"orbit-relay")
        await pilot.pause()
        assert app.screen.query_one("#filter").value == "orbit-relay"

        await pilot.press("ctrl+q")
        await pilot.pause()

        assert app.is_running, (
            "quit left on an arm held over from work that had been cleared since — the owner "
            "retyped the same name and lost it without being warned again"
        )


async def test_quit_on_a_gathered_review_warns_without_naming_an_entry() -> None:
    """The screens that override `work_in_flight` but hold no typed entry.

    `ProjectReviewScreen` calls `hide_entry()` and holds its work as gathered state, so
    `work_at_risk` is empty by construction and the warning falls back to a general sentence
    rather than quoting nothing. That fallback is the whole reason `work_at_risk` returns a
    `str` rather than `str | None`.

    Included because the Tier-1 review pointed out the two `work_in_flight` overrides had no
    coverage here at all: the fallback was correct only because `hide_entry` happens to clear
    the value too, and nothing was holding that precondition in place.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        assert position(app) == "PROJECT_REVIEW", f"expected the review, got {position(app)}"
        assert app.screen.work_in_flight, "the gathered selection is the work at risk here"
        assert app.screen.work_at_risk == "", "a hidden entry must not be quoted as the work"

        await pilot.press("ctrl+q")
        await pilot.pause()

        assert app.is_running, "the gathered selection was discarded with no warning"
        warned = announcements(app, severity="warning")
        assert warned and "what you have built on this screen" in warned[-1], (
            f"the fallback warning did not render; the surface said {warned}"
        )


async def test_quit_warns_again_after_the_work_was_replaced_by_a_paste() -> None:
    """The second Critical, from the same review: a paste is not a key.

    `events.Paste` is not an `events.Key` — it is not even an `events.InputEvent` — and
    `App.on_event` routes it down a separate branch, so a disarm that matched only on keys
    never saw it. `Input._on_paste` can replace a selection with identical clipboard text
    while emitting no key at all, which left the arm standing over work that had been
    destroyed and rebuilt. Re-pasting a name to confirm it is an ordinary thing to do, and it
    quit on a single press.

    Driven by posting the `Paste` the terminal would send, which is what `Input._on_paste`
    consumes; the point is precisely that no key accompanies it.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)

        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running, "the first press should have warned"

        entry = app.screen.query_one("#filter")
        entry.focus()
        await pilot.pause()
        # Select the WHOLE value and replace it by paste with identical text, so the value
        # ends where it started. Selecting less would leave a different value behind, and the
        # signature comparison would then re-warn on its own — which is a test that passes
        # without the disarm and proves nothing. An earlier draft of this test did exactly
        # that and survived the mutation unchanged.
        entry.select_all()
        assert not entry.selection.is_empty, "nothing was selected, so the paste would insert"
        # Posted to the *app*, which is where the driver delivers a bracketed paste and the
        # only path that reaches `App.on_event`. Posting straight to the Input bypasses the
        # app entirely, which is a delivery no terminal performs — and a test doing that
        # measures nothing about this guard.
        app.post_message(events.Paste("orbit-relay"))
        await pilot.pause()
        assert entry.value == "orbit-relay", (
            f"the paste left {entry.value!r}, so this test would re-warn on the changed "
            f"signature rather than on the disarm it exists to check"
        )

        await pilot.press("ctrl+q")
        await pilot.pause()

        assert app.is_running, (
            "a paste replaced the work and the arm survived it, so a single ctrl+q left with "
            "the name gone — the key-only disarm could not see a Paste"
        )


@pytest.mark.parametrize("screen", ALL_SCREENS, ids=lambda s: s.__name__)
def test_quit_can_name_what_every_screen_would_discard(screen: type) -> None:
    """A screen that overrides `work_in_flight` must also answer `work_at_risk`.

    The pairing sweep a Tier-1 review asked for, on the precedent
    `test_every_screen_has_answered_whether_it_can_be_empty` already set for DEC-009. The two
    properties are one contract in two halves — *is* there work, and *what* is it — and the
    quit warning reads both. `ChoiceScreen` documents in prose that overriding one means
    overriding the other; prose is not a check.

    Before this existed, `ProjectReviewScreen` and the launch wizard's own review screen — since
    removed, along with the position it named — both overrode `work_in_flight` to `True` and
    inherited `work_at_risk`. That was correct — but only because `populate`
    happens to call `hide_entry()`, which clears the value the default would otherwise have
    quoted. Correct by an unenforced precondition is the shape DEC-009's own reasoning names
    as worth generalizing a check for: a screen that later grew a visible entry holding
    something unrelated would have quoted it to the owner as the work about to be lost.
    """
    if not issubclass(screen, ChoiceScreen):
        return
    if screen.work_in_flight is ChoiceScreen.work_in_flight:
        return
    assert screen.work_at_risk is not ChoiceScreen.work_at_risk, (
        f"{screen.__name__} overrides work_in_flight but inherits work_at_risk, so the quit "
        f"warning names whatever happens to be in its entry — or nothing — rather than what "
        f"this screen actually holds. Declare work_at_risk, returning '' if the work has no "
        f"nameable value."
    )


def test_quit_disarm_still_covers_every_way_an_input_can_change() -> None:
    """Pin the surface `on_event`'s disarm is a closed-world assumption over.

    `RemoteAgentsTui.on_event` disarms on `events.Key` and `events.Paste` and deliberately not
    on `events.MouseEvent`. **This enumeration has been got wrong twice**, in both Criticals
    this task's review raised — first by omitting the retype path entirely, then by matching
    only `Key` and missing `Paste`, which is not even an `InputEvent`.

    **The first version of this pin was itself too narrow, and a gate evaluator caught it.** It
    asserted `set(events.InputEvent.__subclasses__()) == {Key, MouseEvent}` and claimed in its
    docstring that "a Textual upgrade that adds an input-adjacent event class — IME
    composition, a drag-and-drop text drop — fails here". It would not have. `Paste` is one of
    roughly thirty *direct* `Event` subclasses, so it never appeared under `InputEvent` at all
    — and a future text-drop event, modelled the way `Paste` is, would sail past a pin that
    only constrains the `InputEvent` branch. A guard test overstating its own coverage is the
    exact species this sub-plan exists to eliminate, so it is pinned on the right axis now.

    **The right axis is `Input`'s own handlers**, not the event hierarchy. What the disarm
    actually needs to cover is every way the widget's value can change under the owner's
    hands, and that is decided by which events `Input` chooses to handle — a new event class
    Textual adds but `Input` ignores cannot change anything. So this freezes the handlers
    `Input` itself defines. Two of them mutate `value` today (`_on_key`, `_on_paste`) and both
    are covered; the four mouse handlers touch only selection and cursor, which is why
    `MouseEvent` is excluded from the disarm.

    A Textual upgrade that gives `Input` a new handler — `_on_drop`, an IME composition
    handler — fails here, where the failure names the decision to revisit, rather than
    silently reopening the discard for whoever pastes their project name.
    """
    from textual.widgets import Input

    handlers = {name for name in vars(Input) if name.startswith(("_on_", "on_"))}
    assert handlers == {
        "_on_blur",
        "_on_focus",
        "_on_key",
        "_on_mount",
        "_on_mouse_down",
        "_on_mouse_move",
        "_on_mouse_release",
        "_on_mouse_up",
        "_on_paste",
        "_on_suggestion_ready",
    }, (
        f"`Input`'s handler set has changed: {sorted(handlers)}. "
        f"`RemoteAgentsTui.on_event` disarms the quit warning on Key and Paste only, because "
        f"those are the two handlers that mutate `value` — the mouse handlers touch selection "
        f"and cursor alone. Re-derive that against the new handler before updating this test: "
        f"if it can change what the owner typed, the disarm has to see it."
    )
    assert not issubclass(events.Paste, events.InputEvent), (
        "Paste is now an InputEvent. The disarm names it separately precisely because it was "
        "not one; re-read on_event before changing this."
    )
