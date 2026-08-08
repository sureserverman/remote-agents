"""Every screen that asks for a resting row actually draws the cursor on it.

DEC-007 accepts that a second surface can destroy a session, and one of the mitigations it
names is that a destructive confirm opens with the abort under the cursor: "the abort entry
is first and highlighted, and confirming means moving to a different row on purpose"
(`adapters/tui/app.py`, `_confirm_force`).

Only the first half of that was true. `_fill` set `ListView.index` in the same synchronous
pass that appended the rows, so the highlight was never applied to a mounted child — the
index was right, but no row was ever marked `highlighted` and no cursor was drawn. The
functional safety held (a stray enter still activated the abort, because the index decides
that); what was missing was the owner being able to *see* where the cursor rested while
being asked to confirm an irreversible kill.

So these assert the rendered highlight, never the index. The index was correct throughout
the defect's life, which is exactly why asserting it would have proved nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from textual.widgets import ListItem, ListView

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
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
_SESSION_ID = SessionId.new()


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


def _context() -> TuiContext:
    return TuiContext(
        launcher=_Launcher(),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
    )


def _highlighted(app: RemoteAgentsTui) -> tuple[str | None, list[str]]:
    """The label of the row drawn as the cursor, and every label on screen."""
    rows = [
        (str(item.query_one("Label").content), item.highlighted) for item in app.query(ListItem)
    ]
    marked = [text for text, is_highlighted in rows if is_highlighted]
    assert len(marked) <= 1, f"more than one row is highlighted: {marked}"
    return (marked[0] if marked else None), [text for text, _ in rows]


async def _drive_to_force_confirm(app: RemoteAgentsTui) -> None:
    await app._show_sessions()
    await app._show_detail(str(_SESSION_ID))
    await app._confirm_force()


async def _drive_to_review(app: RemoteAgentsTui) -> None:
    app._choose_project("opaque-existing")
    app._choose_profile("claude")
    app._submit_label("nightly run")


async def _drive_to_project_review(app: RemoteAgentsTui) -> None:
    await app._show_areas()
    await app._choose_area("infra")
    app._submit_name("new-project")


# Each entry is a position whose resting row must be the one that mutates nothing.
_RESTING = (
    pytest.param(_drive_to_force_confirm, "Cancel", Step.FORCE_CONFIRM, id="force-confirm"),
    pytest.param(_drive_to_review, "Back", Step.REVIEW, id="review"),
    pytest.param(_drive_to_project_review, "Back", Step.PROJECT_REVIEW, id="project-review"),
)


@pytest.mark.parametrize("drive,expected,step", _RESTING)
async def test_the_resting_cursor_is_drawn_on_the_non_mutating_row(drive, expected, step) -> None:
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await drive(app)
        await pilot.pause()
        assert app._step is step
        marked, rows = _highlighted(app)
        assert marked is not None, (
            f"{step.name} drew no cursor at all; rows were {rows}. The owner cannot see "
            f"which row an enter would activate."
        )
        assert marked == expected, (
            f"{step.name} rests on {marked!r}, not the non-mutating {expected!r}. Rows were {rows}."
        )


async def test_a_list_with_no_resting_preference_still_draws_a_cursor() -> None:
    """An ordinary list highlights its first row, so the cursor is never invisible."""
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._show_sessions()
        await pilot.pause()
        marked, rows = _highlighted(app)
        assert marked is not None, f"the sessions list drew no cursor; rows were {rows}"
        assert marked == rows[0]


async def test_the_index_and_the_drawn_cursor_agree() -> None:
    """The two halves cannot drift apart again without this failing.

    The defect this file was written for was precisely a disagreement between them: the
    index said row 0, and no row was drawn as row 0.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _drive_to_force_confirm(app)
        await pilot.pause()
        choices = app.query_one("#choices", ListView)
        marked, rows = _highlighted(app)
        assert choices.index is not None
        assert rows[choices.index] == marked


async def test_a_superseded_cursor_placement_stands_down() -> None:
    """A deferred placement declines to act once a later fill has replaced the rows.

    The placement is scheduled after a refresh, so its index was computed against entries
    that may no longer be on screen. `ListView.validate_index` clamps rather than rejects,
    so a stale callback would not error — it would silently rest the cursor on some
    unrelated row of the current list. On a destructive confirm that is exactly the DEC-007
    mitigation being undone with no symptom to notice.

    No production path reaches this today, because every `_fill` caller awaits fully between
    fills. It is pinned now because the next stage moves these handlers onto workers, which
    is what would make it reachable.

    `_rest_cursor` is invoked directly rather than by racing two fills through the message
    pump: the guard's contract is "act only for the newest fill", and driving that through
    two interleaved mount/remove cycles tests the pump's scheduling instead — a version of
    this test that did so failed 2 runs in 8.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._fill((("a", "alpha"), ("b", "beta"), ("c", "gamma")), highlight=2)
        superseded = app._resting_generation
        await pilot.pause()

        app._fill((("x", "one"), ("y", "two")), highlight=0)
        await pilot.pause()
        current = app._resting_generation
        assert current != superseded, "each fill must take its own generation"

        choices = app.query_one("#choices", ListView)
        marked_before, rows = _highlighted(app)
        assert rows == ["one", "two"]

        # The superseded fill's index (2) clamps onto the two-row list at row 1 -- "two",
        # the row it must not reach.
        app._rest_cursor(choices, 2, superseded)
        await pilot.pause()
        marked_after, _ = _highlighted(app)
        assert marked_after == marked_before == "one", (
            f"a superseded placement moved the cursor to {marked_after!r}"
        )

        # The current generation is still honoured, so the guard blocks staleness only.
        app._rest_cursor(choices, 1, current)
        await pilot.pause()
        marked_current, _ = _highlighted(app)
        assert marked_current == "two", "the guard must not block the newest fill"
