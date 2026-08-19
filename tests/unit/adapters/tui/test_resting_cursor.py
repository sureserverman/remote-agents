"""Every screen that asks for a resting row actually draws the cursor on it.

DEC-007 accepts that a second surface can destroy a session, and one of the mitigations it
names is that a destructive confirm opens with the abort under the cursor: "the abort entry
is first and highlighted, and confirming means moving to a different row on purpose"
(`adapters/tui/screens/sessions.py`, `SessionDetailScreen.confirm_force`).

Only the first half of that was true. `_fill` set the cursor index in the same synchronous
pass that appended the rows, so the highlight was never applied to a mounted child — the
index was right, but no row was ever marked `highlighted` and no cursor was drawn. The
functional safety held (a stray enter still activated the abort, because the index decides
that); what was missing was the owner being able to *see* where the cursor rested while
being asked to confirm an irreversible kill.

So these assert the rendered highlight, never the index. The index was correct throughout
the defect's life, which is exactly why asserting it would have proved nothing.

That distinction survives the move to `OptionList` and is why `_highlighted`
below reads rendered strips rather than the widget's state. (The widget it replaced is not
named here on purpose: that name is swept for across `src/` and `tests/` as migration residue,
and a sweep that has to carve out prose exceptions stops being run.) `OptionList` keeps one
reactive, `highlighted`, and `render_line` styles a row by comparing it against that reactive — so
reading `highlighted` back is reading the index again, under a new name. The two-variable
disagreement this file was written for cannot happen in this widget, but the assertion that
would have caught it is the one worth keeping, because it is the only one that fails if the
cursor stops being *painted* for some other reason.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from test_tui_snapshots import settle
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()
_REFERENCE = ConversationReference("c-" + "0" * 14 + "01")


def _record() -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Launcher:
    record: SessionRecord = field(default_factory=_record)

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None


@dataclass(slots=True)
class _Creator:
    def available_areas(self):
        return ("dev-area", "infra")


def _summary() -> ConversationSummary:
    return ConversationSummary(
        _REFERENCE,
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )


class _Conversations:
    """Wired so the resume flow's commit point can be driven to, and only for that."""

    async def catalogue(self, query):
        return ConversationCataloguePage((_summary(),), query.page, 1)

    async def resolve_for_resume(self, _reference):
        return ResolvedConversation(_summary(), None)  # type: ignore[arg-type]

    async def capabilities(self):
        return (
            ProfileResumeCapability(
                ProfileId("claude"), catalogue_available=True, selected_resume_available=True
            ),
        )


def _context() -> TuiContext:
    return TuiContext(
        launcher=_Launcher(),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        conversations=_Conversations(),  # type: ignore[arg-type]
    )


def _highlighted(app: RemoteAgentsTui) -> tuple[str | None, list[str]]:
    """The text of the row drawn as the cursor, and every row's text.

    Read off the rendered strips, for the reason in this module's docstring: the cursor is
    what `render_line` paints, and painting it is the half of DEC-007's mitigation that was
    missing. `option-list--option-highlighted` is the component class `render_line` reaches
    for when the row it is drawing is the highlighted one, so resolving that style and looking
    for it in the output asks the widget what it drew rather than what it intended to draw.

    `clear_meta_and_links` before comparing, because the segments that come back carry the
    row's mouse-hit metadata (`meta={'option': 1, ...}`) and rich's `Style.__eq__` compares
    that too — so an unstripped comparison is never equal, and this helper would report that
    no cursor was drawn on every screen rather than failing on its own bug.
    """
    choices = app.screen.query_one("#choices", OptionList)
    rows = [str(option.prompt) for option in choices.options]
    cursor = choices.get_visual_style(
        "option-list--option", "option-list--option-highlighted"
    ).rich_style.clear_meta_and_links()
    marked: list[str] = []
    for line in range(choices.scrollable_content_region.height):
        strip = choices.render_line(line)
        painted = [segment for segment in strip if segment.text.strip()]
        if not painted:
            continue
        if all(segment.style.clear_meta_and_links() == cursor for segment in painted):
            marked.append("".join(segment.text for segment in strip).strip())
    assert len(marked) <= 1, f"more than one row is drawn as the cursor: {marked}"
    return (marked[0] if marked else None), rows


async def _drive_to_force_confirm(app: RemoteAgentsTui) -> asyncio.Task[None]:
    """Open the force confirmation and leave it open, suspending the caller that asked.

    It is a modal now, so `confirm_force` does not return until the question is answered —
    the drive hands back the suspended task and the test joins it once it has looked at the
    cursor. Answering first would close the very screen being examined.
    """
    await app.show_sessions()
    await app.show_detail(str(_SESSION_ID))
    return asyncio.create_task(app.screen.confirm_force())


async def _drive_to_review(app: RemoteAgentsTui) -> None:
    # Through each screen's own handler, so the cursor under test is the one the real
    # navigation leaves behind rather than one a directly-built screen happens to draw.
    # Two choices, not three: the agent choice lands on the review directly since the launch
    # flow lost its label step.
    await app.screen.choose("opaque-existing")
    await app.screen.choose("launch")
    await app.screen.choose("claude")


async def _drive_to_remote_control_confirm(app: RemoteAgentsTui) -> asyncio.Task[None]:
    """Same shape as the force drive, and modal for the same reason."""
    await app.show_sessions()
    await app.show_detail(str(_SESSION_ID))
    return asyncio.create_task(app.screen.confirm_remote_control(RemoteControlState.ACTIVE))


async def _drive_to_the_conversation_list(app: RemoteAgentsTui) -> None:
    """The resume flow's commit position, which is the list itself now.

    It used to be a confirmation standing after this screen, and this file had an entry for it
    with the same "abort under the cursor" shape as the two destructive confirms. Removing that
    screen made choosing a row here the act, and took the entry with it — so for a while the
    resume flow's commit point rested on the mutating row and nothing in this file noticed. A
    gate evaluator caught it by pressing enter twice from the agent list.
    """
    await app.action_resume()
    await app.screen.choose("opaque-existing")
    await app.screen.choose("claude")


async def _drive_to_project_review(app: RemoteAgentsTui) -> None:
    await app.show_areas()
    await app.screen.choose("infra")
    app.screen.submit("new-project")


async def _drive_to_session_detail(app: RemoteAgentsTui) -> None:
    """The detail itself, which this file never listed while it grew mutating rows.

    The two modal drives above pass straight through this position to reach their
    confirmations, so it has been *traversed* by this file since the day it was written and
    never *asserted*. Its resting row was pinned only by the committed SVG baselines — real
    coverage, and the argument this file's own prose makes for why a painted-highlight
    assertion is worth keeping, but the baselines are a net for rendering rather than a
    statement about which row an enter activates.

    Added when a gate evaluator noticed that the detail now rests one Down away from an
    unconfirmed Clean up on a PRESERVED session: the cursor resting on a read is the whole
    reason a stray enter there is harmless, and nothing in this file said so.
    """
    await app.show_sessions()
    await app.show_detail(str(_SESSION_ID))


# Each entry is a position whose resting row must be the one that mutates nothing.
_RESTING = (
    pytest.param(_drive_to_force_confirm, "Cancel", "FORCE_MODAL", id="force-confirm"),
    # The second destructive confirm. Added because DEC-007's abort-rests-under-the-cursor
    # mitigation was only ever checked here on the force path, so the Remote Control confirm
    # had its row order asserted but never the cursor actually painted on it — which is the
    # exact distinction this file exists to draw.
    pytest.param(
        _drive_to_remote_control_confirm,
        "Cancel",
        "REMOTE_CONTROL_MODAL",
        id="remote-control-confirm",
    ),
    pytest.param(_drive_to_review, "Back", "REVIEW", id="review"),
    # The resume flow's commit position. Restored after the confirmation it used to sit on was
    # removed: the rows here lead nowhere else now, so the cursor must not rest on one.
    pytest.param(
        _drive_to_the_conversation_list, "Back", "RESUME_CONVERSATIONS", id="resume-conversations"
    ),
    pytest.param(_drive_to_project_review, "Back", "PROJECT_REVIEW", id="project-review"),
    # Not a confirmation, and the only entry here that is not: this position offers reads and
    # stops side by side with no separator, so which row the cursor rests on is what decides
    # whether a repeated enter reads a session or ends one.
    pytest.param(_drive_to_session_detail, "Copy attach", "SESSION_DETAIL", id="session-detail"),
)


@pytest.mark.parametrize("drive,expected,step", _RESTING)
async def test_the_resting_cursor_is_drawn_on_the_non_mutating_row(drive, expected, step) -> None:
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # A drive that opens a modal hands back the suspended caller; every other one
        # returns None. Joined at the end of the test rather than here, because a modal
        # answered is a modal already gone from the screen this is about to read.
        asking = await drive(app)
        await settle(app, pilot)
        assert position(app) == step
        marked, rows = _highlighted(app)
        assert marked is not None, (
            f"{step} drew no cursor at all; rows were {rows}. The owner cannot see "
            f"which row an enter would activate."
        )
        assert marked == expected, (
            f"{step} rests on {marked!r}, not the non-mutating {expected!r}. Rows were {rows}."
        )
        # The row an enter would activate, tied to the row the owner can see. `action_select`
        # reads `highlighted`, so this is the *enter target*, and asserting it against
        # `expected` rather than against `marked` is what keeps the check from being circular:
        # `render_line` paints the highlight by comparing `highlighted` to the row index, so
        # "the drawn row and the reactive agree" is true by construction and proves nothing.
        # Landing on the wrong row is the failure this catches, and it is reachable.
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.highlighted is not None
        assert rows[choices.highlighted] == expected

        if asking is not None:
            await pilot.press("escape")
            await asyncio.wait_for(asking, timeout=5)


async def test_a_list_with_no_resting_preference_still_draws_a_cursor() -> None:
    """An ordinary list highlights its first row, so the cursor is never invisible."""
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await settle(app, pilot)
        marked, rows = _highlighted(app)
        assert marked is not None, f"the sessions list drew no cursor; rows were {rows}"
        assert marked == rows[0]


# `test_the_index_and_the_drawn_cursor_agree` was deleted here, and the deletion is the point.
#
# It asserted `rows[choices.highlighted] == marked` — that the reactive and the painted row
# agree. Under the widget this file was originally written against, that was the whole defect:
# an index said row 0 while no row was drawn as row 0, because the highlight was a per-row
# class applied to a mounted child and the index was a separate number. `OptionList` keeps a
# single reactive, and `render_line` paints the highlight by comparing it to the row index —
# so the two cannot disagree, and the assertion could not fail for any reason the tests above
# would not also catch. It had become a test of Textual's internal consistency.
#
# Its one real value — that the *enter target* is the row the owner sees — is folded into the
# parametrized test above, where it is asserted against the expected non-mutating label rather
# than against the drawn row. That version can fail, which is the difference.


async def test_a_superseded_cursor_placement_stands_down() -> None:
    """A deferred placement declines to act once a later fill has replaced the rows.

    The placement is scheduled after a refresh, so its index was computed against entries
    that may no longer be on screen. `OptionList.validate_highlighted` clamps rather than
    rejects, so a stale callback would not error — it would silently rest the cursor on some
    unrelated row of the current list. On a destructive confirm that is exactly the DEC-007
    mitigation being undone with no symptom to notice.

    **Corrected after review:** this paragraph used to say no production path reached it,
    "because every `_fill` caller awaits fully between fills", and that the next stage's move
    to workers is what would make it reachable. That was wrong when written. `_show_areas`
    and the catalogue refresh already awaited off the event loop through `asyncio.to_thread`,
    and an `await` yields to the pump the same way a worker await does — the interleaving
    hazard predates the worker migration rather than being created by it.

    `_rest_cursor` is invoked directly rather than by racing two fills through the message
    pump: the guard's contract is "act only for the newest fill", and driving that through
    two interleaved mount/remove cycles tests the pump's scheduling instead — a version of
    this test that did so failed 2 runs in 8.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.show_choices((("a", "alpha"), ("b", "beta"), ("c", "gamma")), highlight=2)
        superseded = app.screen._resting_generation
        await pilot.pause()

        app.screen.show_choices((("x", "one"), ("y", "two")), highlight=0)
        await pilot.pause()
        current = app.screen._resting_generation
        assert current != superseded, "each fill must take its own generation"

        choices = app.screen.query_one("#choices", OptionList)
        marked_before, rows = _highlighted(app)
        assert rows == ["one", "two"]

        # The superseded fill's index (2) clamps onto the two-row list at row 1 -- "two",
        # the row it must not reach.
        app.screen._rest_cursor(choices, 2, superseded)
        await pilot.pause()
        marked_after, _ = _highlighted(app)
        assert marked_after == marked_before == "one", (
            f"a superseded placement moved the cursor to {marked_after!r}"
        )

        # The current generation is still honoured, so the guard blocks staleness only.
        app.screen._rest_cursor(choices, 1, current)
        await pilot.pause()
        marked_current, _ = _highlighted(app)
        assert marked_current == "two", "the guard must not block the newest fill"


async def test_one_key_from_the_resting_row_reaches_the_first_conversation() -> None:
    """The resting cursor is only affordable because Down wraps, so that is pinned too.

    Resting the conversation list on Back is safe, and it is *cheap* only because Textual's
    `OptionList.action_cursor_down` routes through `find_next_enabled`, which wraps from the
    last row to the first — so one Down reaches conversation 1 whether the page holds one row
    or twelve plus paging rows. `find_next_enabled_no_wrap` ships beside it.

    Without this, a future widget change turns "one key" into "a page-length of keys" and every
    other test still passes: the safety property this file asserts would hold while the
    affordance that made it acceptable had quietly gone.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _drive_to_the_conversation_list(app)
        await settle(app, pilot)
        assert position(app) == "RESUME_CONVERSATIONS"

        marked, rows = _highlighted(app)
        assert marked == "Back", f"the fixture did not start on the resting row; rows were {rows}"

        await pilot.press("down")
        await pilot.pause()
        landed, rows = _highlighted(app)

    assert landed == rows[0], (
        f"one Down from the resting row landed on {landed!r}, not the first conversation "
        f"{rows[0]!r} — the cursor no longer wraps, so reaching a conversation now costs a "
        f"keypress per row and resting on Back has become expensive"
    )


async def test_the_dashboard_sessions_pane_rests_its_cursor_and_answers_bare_keys() -> None:
    """The pane advertises "enter opens, d for detail"; both must work with no prior
    arrow press, purely from real keys — a pane with no resting cursor makes them silent
    no-ops, which a test that sets `highlighted` by hand can never see. Found by the
    Stage 4 Tier-2 review with a live Pilot repro."""
    from remote_agents.adapters.tui.screens.dashboard import DashboardScreen
    from remote_agents.adapters.tui.screens.sessions import SessionDetailScreen

    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        pane = app.screen.query_one("#sessions-pane", OptionList)
        assert pane.highlighted is not None, "the pane must rest its cursor on first fill"

        pane.focus()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, SessionDetailScreen), (
            "d with no prior arrow press must open the detail"
        )
