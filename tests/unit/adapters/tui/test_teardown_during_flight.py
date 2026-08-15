"""Quitting while a project create is in flight, and what its failure path does afterwards.

BL-014's claim, in the words it was deferred with: quitting the app while a project-create
call is in flight can let an exception escape — a `MountError` out of the *error-recovery*
path, i.e. out of the code that exists to put a failure on screen without losing the app. It
was deferred on the argument that the Textual screen rewrite would fix it structurally. That
rewrite has shipped and nobody had checked. This file is the check.

**What "in flight" can end as, and why this file covers only one of the two endings.**
Every awaited call in the create flow goes through `RemoteAgentsTui.in_thread`, and a quit
resolves it one of two ways:

* *Cancelled.* `App` cancels its workers before it prunes its screens, so the ordinary
  ending of a quit is `WorkerCancelled`, which `in_thread` re-raises as `CancelledError` —
  a `BaseException`, so it passes straight through every `except Exception` in this flow and
  the recovery path never runs at all. That ending is already pinned, by
  `test_tui_worker_exclusivity.py`'s
  `test_a_cancelled_read_does_not_render_an_error_into_a_dying_screen`.
* *Failed.* The worker raises for its own reasons — a read-only development root, a registry
  that will not append — at the moment the owner presses ctrl+q. Then `in_thread` re-raises
  the real exception, `except Exception` catches it, and the handler tries to report a
  failure to a screen that is on its way out. **That is BL-014's ending, and nothing covered
  it.**

So the two endings are a race in production and a parameter here. `in_thread` is replaced by
a stand-in that suspends on the call under test and then raises, which turns "the failure
landed after the teardown" from something to hope for into a synchronisation point — the
same reason `_SlowLauncher` in `test_tui_worker_exclusivity.py` uses events rather than
sleeps. Nothing else about the flow is stubbed: the real screens, the real handlers, the real
`except` branches and the real guards run.

**Non-vacuity is asserted, not assumed.** A test that quits, sees no exception and passes
cannot tell "the guard held" from "the error path never ran". Every case here wraps the
screen's own `announce` — the reporting call inside the `except` branch — and asserts on the
one sentence that branch produces, so the file fails if the recovery path is skipped.

Verified by mutation while it was written, twice, because the three cases do not all lean on
the same guard:

* Deleting `if not self.showing: return` from `ChoiceScreen._set_working` reddens the `create`
  and `catalogue` cases with `NoMatches: No nodes match '#choices' on ProjectReviewScreen()`
  escaping the handler. It leaves `areas` green, correctly: `AreasScreen.populate` reports
  without opening `awaiting`, so it never reaches `_set_working` at all.
* Deleting the same guard from `ChoiceScreen.set_status` reddens all three, which is the one
  render every recovery path in this flow makes.

The guards in `screens/base.py` are what make this file green; they are not decoration.

**Two arrangements, and the difference matters — corrected at the Stage 1 gate.** The three
cases above run the handler on `asyncio.create_task`, which detaches it from the message
pump. That detachment is what lets the shutdown finish while the call is parked, and
therefore what *produces* the pruned screen they report against. **The real surface does not
do this.** `ProjectReviewScreen.choose` is awaited inline from
`ChoiceScreen.on_option_list_option_selected`, so it runs on the screen's own pump, and
`App._close_all` waits for that pump — park a call there and the shutdown cannot proceed at
all. The last test in this file drives that path and measures it.

The first version of this file claimed the opposite in this docstring and built a premise
test around it, asserting the pruned screen was what the owner's own path produced. It is
not. The distinction is worth stating rather than quietly fixing, because the conclusion
survived and the evidence given for it did not — and those are separable things.

**Result: BL-014 does not reproduce**, and now for two independent reasons rather than one.
On the real pump path the state it feared is *unreachable*: the teardown is serialised behind
the handler, so a failure cannot land on a pruned screen. Constructed off-pump, where it can,
the guards in `screens/base.py` hold it anyway. There is no `xfail` in this file. What it
defends is the guarantee rather than the defect — and both halves are worth keeping, since
the guards are what a future off-pump caller would need and the pump ordering is why no
present one does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from test_tui_snapshots import settle
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")

#: What the stand-in raises. An ordinary `Exception`, deliberately: `CancelledError` is the
#: *other* ending of the race and is covered elsewhere, and the whole point of this file is
#: the branch a `BaseException` would skip.
_BOOM = "the development root is read-only"


class _Creator:
    """The real shape of the create collaborator; failure is injected at `in_thread`.

    Both methods succeed. Which call fails, and *when* relative to the teardown, is decided
    by `_Suspending` below rather than here — a creator that raised on its own would fail
    while the app was still up, which is the case this file is not about.
    """

    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


def _context() -> TuiContext:
    return TuiContext(
        launcher=object(),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
    )


@dataclass
class _Suspending:
    """Stands in for `RemoteAgentsTui.in_thread`, holding one call open until released.

    `armed` is the `group` label of the call under test — `"areas"`, `"create-project"` or
    `"catalogue"`, the three this flow passes. Every other group is handed to the real
    `in_thread` and runs as it always does, so a case that suspends the catalogue re-read
    still creates the project through the real worker first.

    The armed call sets `started`, waits, and then raises. `started`/`release` make the
    window a synchronisation point rather than a wall-clock guess: the test can quit the app,
    let its screens be pruned, and only *then* let the failure land. That ordering is the
    whole experiment, and a `sleep` long enough to arrange it on an idle machine is the flake
    `test_tui_worker_exclusivity.py` already records paying for.
    """

    real: Callable[..., Any]
    armed: str
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    groups: list[str] = field(default_factory=list)

    async def __call__(self, work: Callable[[], Any], *, group: str) -> Any:
        self.groups.append(group)
        if group != self.armed:
            return await self.real(work, group=group)
        self.started.set()
        await self.release.wait()
        raise RuntimeError(_BOOM)


def _watch_announcements(screen: ChoiceScreen, into: list[str]) -> None:
    """Record what the screen reports, and let the real report happen anyway.

    Wrapping rather than replacing, because the assertion is "the `except` branch ran" and
    not "the `except` branch was prevented from running": the real `announce` still has to
    execute, since it is one of the calls that could raise out of a torn-down screen. The
    recorded text is what makes the check specific — each branch below produces one sentence
    no other branch produces, so this cannot be satisfied by some unrelated toast.
    """
    real = screen.announce

    def recording(message: str, **keywords: Any) -> None:
        into.append(message)
        real(message, **keywords)

    screen.announce = recording  # type: ignore[method-assign]


async def _select(app: RemoteAgentsTui, key: str) -> None:
    """Deliver a row selection the way a keypress does — through the real handler.

    The same reason `test_tui_worker_exclusivity.py` routes through the handler rather than
    calling `choose`: the busy refusal lives in `ChoiceScreen.on_option_list_option_selected`,
    and a test that steps around it is testing a path the owner cannot reach.
    """
    choices = app.screen.query_one("#choices", OptionList)
    index = choices.get_option_index(key)
    await app.screen.on_option_list_option_selected(
        OptionList.OptionSelected(choices, choices.get_option_at_index(index), index)
    )


async def _drive_to_the_review(app: RemoteAgentsTui, pilot) -> None:
    """Area, name, review — the two positions before anything is created."""
    await app.show_areas()
    await settle(app, pilot)
    await app.screen.choose("infra")
    await pilot.pause()
    app.screen.submit("brand-new")
    await settle(app, pilot)


@pytest.mark.parametrize(
    "group,reaches",
    [
        pytest.param("areas", "The development root could not be read", id="available-areas"),
        pytest.param("create-project", "Project not created", id="create"),
        pytest.param("catalogue", "the project catalogue could not be re-read", id="catalogue"),
    ],
)
async def test_a_failure_landing_after_the_quit_escapes_no_exception(group, reaches) -> None:
    """BL-014, one awaited call at a time: quit mid-flight, then let the call fail.

    The three parameters are every awaited call the create flow makes, in the order the owner
    meets them:

    * `areas` — `AreasScreen.populate`'s read of the development root.
    * `create-project` — `ProjectReviewScreen.choose`'s create.
    * `catalogue` — `RemoteAgentsTui.reload_catalogue`, awaited by that same `choose` after
      the project exists. Its own `except` swallows the exception and answers `False`, so the
      branch under test is the caller's partial-success one, which is the *only* recovery
      path here that runs after something was really written to disk.

    Each case: suspend that call, quit, let the app tear its screens down, then release. The
    handler resumes into an `except` branch whose screen has been pruned out from under it —
    `showing` is `False`, `query_one` on any of its widgets would raise `NoMatches` — and the
    two assertions are that the branch was nonetheless *entered*, and that nothing came back
    out of it.

    The release deliberately happens **after** `run_test`'s context has closed. That is what
    puts the failure on the far side of `App._shutdown`; releasing inside the block leaves
    the screen mounted and the guards untouched, which is a different and much weaker test —
    measured while writing this one, where removing a guard changed nothing at all.

    **This arrangement is constructed, not observed.** Running the handler on a task is what
    detaches it from the pump and so lets the shutdown complete underneath it; on the real
    path the pump holds the teardown and none of this ordering is reachable (see the last
    test in this file, and the module docstring's note on the two arrangements). What these
    three cases establish is therefore conditional and worth having in that form: *if* a
    failure ever reaches a pruned screen — which a future caller running off the pump could
    arrange — the guards in `screens/base.py` contain it.
    """
    app = RemoteAgentsTui(_context())
    reported: list[str] = []
    escaped: list[str] = []
    suspend: _Suspending | None = None
    in_flight: asyncio.Task[None] | None = None

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        if group == "areas":
            # The areas read is the flow's first awaited call, and it is reached by *mounting*
            # the screen — `on_mount` awaits `populate`, on the screen's own message pump,
            # which `App._close_all` waits for. Suspending it there deadlocks the shutdown
            # rather than testing it (measured: `await app.show_areas()` never returns). So
            # the screen is mounted normally first and `populate` is re-run on a task: the
            # same awaited call in the same method, driven the way this suite drives every
            # other screen coroutine.
            await app.show_areas()
            await settle(app, pilot)
        else:
            await _drive_to_the_review(app, pilot)
        suspend = _Suspending(app.in_thread, group)
        app.in_thread = suspend  # type: ignore[method-assign]
        _watch_announcements(app.screen, reported)
        if group == "areas":
            in_flight = asyncio.create_task(app.screen.populate())
        else:
            in_flight = asyncio.create_task(_select(app, "create"))

        await asyncio.wait_for(suspend.started.wait(), timeout=5)
        assert group in suspend.groups, (
            f"{group!r} was never reached, so nothing was in flight when the app quit; "
            f"the flow called {suspend.groups}"
        )
        app.exit(None)

    # Outside the context on purpose: the app has shut down and its screens are pruned, so
    # this is the failure arriving at a position that no longer exists.
    suspend.release.set()
    try:
        await in_flight
    except BaseException as error:  # noqa: BLE001 - the escape is the finding, whatever it is
        escaped.append(f"{type(error).__name__}: {error}")

    # Checked **before** the escape, because the order is what keeps the pair honest: a
    # recovery path that never ran escapes nothing, so asserting the absence of an exception
    # first would let a silently skipped branch pass as a guarantee. `escaped` is quoted here
    # too, since one way to reach this line is a render that raised *before* the report —
    # which is a failure of the same defect, arriving one statement earlier.
    assert reported and reaches in reported[-1], (
        f"the `except` branch for {group!r} did not reach its report — the surface said "
        f"{reported} and {escaped or 'nothing'} escaped. Without this the assertion below "
        f"would pass on a flow whose recovery path never ran, which is the vacuity BL-014's "
        f"plan named."
    )
    assert escaped == [], (
        f"reporting a {group!r} failure into a torn-down screen raised {escaped}. BL-014 is "
        f"live: the recovery path is what takes the app down."
    )


async def test_the_screen_really_is_gone_when_the_failure_lands() -> None:
    """The premise the three cases rest on, checked rather than assumed.

    Every assertion above is about a recovery path running against a screen that has been
    torn down. If the screen were still mounted and `showing` still `True`, the same three
    cases would pass with every guard in `screens/base.py` deleted — they would be asserting
    that rendering into a live screen works, which nobody doubts.

    So this pins the premise: after the app has quit, the review screen is off the stack and
    `showing` answers `False`. It is the reason a mutation of `_set_working` is visible from
    here at all.

    **Scope, corrected at the Stage 1 gate: this is the premise of the three cases above, not
    of the production flow.** It holds because the handler was detached onto a task, exactly
    as they detach it. Read as a claim about what the owner's own path produces it would be
    false, and the first version of this file did read that way.

    Asked from a **task created inside** the running app, exactly as the suspended handler is,
    and that is not a stylistic choice. `showing` reaches `self.app`, which resolves Textual's
    `active_app` context variable and falls back to walking `_parent` — and a pruned screen has
    no parent left to walk to, so asking from the plain test body (outside the context
    `run_test` establishes) raises `NoActiveAppError` and would report a hazard belonging to
    the test rather than to the surface. A task inherits the context it was created in, which
    is what makes the in-flight handler's own view of `showing` the one measured here.
    """
    app = RemoteAgentsTui(_context())
    quit_completed = asyncio.Event()
    answers: dict[str, bool] = {}

    async def ask_after_the_teardown(review: ChoiceScreen) -> None:
        await quit_completed.wait()
        answers["showing"] = review.showing
        answers["stacked"] = review in app.screen_stack

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _drive_to_the_review(app, pilot)
        review = app.screen
        assert review.position == "PROJECT_REVIEW"
        assert review.showing, "the review should be the position the owner is looking at"
        asking = asyncio.create_task(ask_after_the_teardown(review))
        app.exit(None)

    quit_completed.set()
    await asyncio.wait_for(asking, timeout=5)

    assert answers["showing"] is False, (
        "the review screen still reports itself as showing after the app quit, so the three "
        "teardown cases are not testing a torn-down screen at all"
    )
    assert answers["stacked"] is False, (
        "the screen stack survived the shutdown; `showing` must be answering `False` for "
        "some other reason than the teardown this file is about"
    )


async def test_on_the_real_pump_path_the_failure_lands_while_the_screen_is_still_up() -> None:
    """What the *production* arrangement does, which is not what the cases above arrange.

    Added by the Stage 1 gate's second review pass, which found that the file was sound about
    its guards and wrong about its premise — a distinction worth the extra test, because the
    conclusion it reached survives and the evidence it gave for it did not.

    Every case above runs the handler on `asyncio.create_task`. That is what lets the
    shutdown finish while the call is parked, and it is therefore what produces the pruned
    screen those cases report against. The real surface does not do this:
    `ProjectReviewScreen.choose` is awaited inline from
    `ChoiceScreen.on_option_list_option_selected`, so it runs **on the screen's own message
    pump** — and `App._close_all` waits for that pump. Park a call there and the shutdown
    cannot proceed at all.

    So this drives the selection the way the framework does, by posting the message and
    letting the pump dispatch it, and measures the ordering that actually ships:

    * with the create parked, `app.exit()` cannot advance — the screen is still stacked, and
      that assertion is deterministic rather than timed, because a blocked pump cannot
      process the shutdown at all;
    * when the failure lands, the screen is still mounted and `showing` is still `True`;
    * nothing escapes.

    **This is the stronger half of BL-014's answer.** The cases above show the guards hold
    *if* a failure ever reaches a pruned screen; this shows that on the path the owner
    actually takes, it cannot — the pump serialises the teardown behind the handler, so the
    state BL-014 feared is unreachable rather than merely survivable. Both are worth keeping:
    the guards are what a future off-pump caller would need, and this is why no present one
    needs them.
    """
    app = RemoteAgentsTui(_context())
    reported: list[tuple[str, bool, bool]] = []
    stacked_while_parked: bool | None = None

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _drive_to_the_review(app, pilot)
        review = app.screen
        suspend = _Suspending(app.in_thread, "create-project")
        app.in_thread = suspend  # type: ignore[method-assign]

        # Records the screen's state *at the moment the recovery path speaks*, which is the
        # whole measurement — asking afterwards would report the state after the pump
        # resumed and the shutdown completed, i.e. a different instant than the one BL-014
        # is about.
        real = review.announce

        def recording(message: str, **keywords: Any) -> None:
            reported.append((message, review.showing, review in app.screen_stack))
            real(message, **keywords)

        review.announce = recording  # type: ignore[method-assign]

        choices = review.query_one("#choices", OptionList)
        index = choices.get_option_index("create")
        # Posted rather than awaited: this is the one difference from `_select`, and it is
        # the difference the whole test is about — the pump dispatches the handler, so the
        # handler holds the pump.
        review.post_message(
            OptionList.OptionSelected(choices, choices.get_option_at_index(index), index)
        )

        await asyncio.wait_for(suspend.started.wait(), timeout=5)
        app.exit(None)

        # The discriminating probe, and the reason this test is not just the cases above
        # with a different spelling. `pilot.pause()` waits for the app to finish processing
        # what it has pending — which it cannot do while this very handler is parked on the
        # pump. Asserted as a timeout in the *expecting-timeout* direction on purpose: a
        # slower machine can only make a blocked pump look more blocked, never less, so this
        # cannot flake toward a false pass. The `await pumped` after the release is what
        # rules out the other reading, that `pause` was hanging for some unrelated reason.
        pumped = asyncio.create_task(pilot.pause())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pumped), timeout=0.5)
        stacked_while_parked = review in app.screen_stack

        suspend.release.set()
        await pumped
        await settle(app, pilot)

    assert stacked_while_parked is True, (
        "the review screen left the stack while a create was still parked on its pump, so "
        "the shutdown is not serialised behind the handler after all — which would make the "
        "pruned-screen state the cases above construct reachable in production too"
    )
    assert reported and "Project not created" in reported[-1][0], (
        f"the `except` branch did not reach its report on the real pump path; the surface "
        f"said {[message for message, _, _ in reported]}"
    )
    _, showing_when_reported, stacked_when_reported = reported[-1]
    assert (showing_when_reported, stacked_when_reported) == (True, True), (
        "on the real pump path the failure is supposed to land while the screen is still up "
        f"— it reported with showing={showing_when_reported}, stacked={stacked_when_reported}"
    )
    assert app._exception is None, (
        f"an exception escaped the recovery path on the real pump path: {app._exception!r}"
    )
