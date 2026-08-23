"""Selecting a session opens a detail view that explains what it is."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from backends import backend_for
from tui_feedback import breadcrumb
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import explain_state
from remote_agents.application.session_views import with_project_names
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(
    state: SessionState = SessionState.RUNNING,
    *,
    slug: str = "opaque-existing",
    custom_label: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(slug, "claude", "regular", 1, custom_label),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing:
    records: tuple[SessionRecord, ...] = ()

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


# ENDED is deliberately unreachable in detail: it is filtered from the listing, so there is
# no row to select. Its explanation is still pinned, by tests/unit/application/
# test_state_explanations.py, which enumerates all 7 members.
_REACHABLE = [state for state in SessionState if state is not SessionState.ENDED]


@pytest.mark.parametrize("state", _REACHABLE)
async def test_detail_explains_every_reachable_lifecycle_state(state: SessionState) -> None:
    """Enumerated over the enum, so an unclassified state fails here rather than shipping."""
    record = _record(state)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert explain_state(state, None) in status


async def test_every_state_is_either_reachable_in_detail_or_deliberately_filtered() -> None:
    """Guards the exclusion above from silently growing to hide an unhandled state."""
    assert set(_REACHABLE) | {SessionState.ENDED} == set(SessionState)


async def test_an_ended_session_has_no_detail_to_open() -> None:
    """Filtered from the list, so selecting it can only mean it ended a moment ago."""
    record = _record(SessionState.ENDED)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert "no longer available" in status.casefold()


async def test_detail_names_the_session_and_its_state() -> None:
    """Both halves are still said; the status split decided *where*.

    The session's name is the header's breadcrumb — it is true of the whole position — and
    the state is the status line, which is what changes underneath it. Asserting both here
    rather than dropping one is the point: a split that quietly stopped naming the session
    would pass a test that had been narrowed to the state.
    """
    record = _record(SessionState.PRESERVED)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)
        trail = breadcrumb(app)

    (named,) = with_project_names((record,), (_EXISTING,))
    assert named.display.rendered in trail
    assert record.display.project_slug not in trail
    assert "preserved" in status


async def test_escape_returns_from_detail_to_the_list() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL"
        await app.action_back()
        await pilot.pause()
        step = position(app)

    assert step == "SESSIONS"


async def test_a_session_that_vanished_between_list_and_detail_does_not_raise() -> None:
    """The store is shared; a session can be stopped elsewhere while this list is open."""
    listed = _record()
    launcher = _Listing((listed,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        launcher.records = ()
        await app.show_detail(str(listed.session_id))
        await pilot.pause()
        status = _status(app)

    assert "no longer available" in status.casefold()


async def test_selecting_a_row_opens_its_detail() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        step = position(app)
        trail = breadcrumb(app)

    assert step == "SESSION_DETAIL"
    (named,) = with_project_names((record,), (_EXISTING,))
    assert named.display.rendered in trail
    assert record.display.project_slug not in trail


# The project the breadcrumb names -------------------------------------------------------------


async def test_the_breadcrumb_names_the_project_rather_than_the_catalogue_id() -> None:
    """The detail is reached from a row that now reads the project's name, so a detail still
    showing the hex prefix would rename the session on the way in -- the owner would arrive
    somewhere that looks like a different session from the one they chose."""
    record = _record(slug="opaque-existing")
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        trail = breadcrumb(app)

    assert "existing" in trail
    assert "opaque-existing" not in trail


async def test_a_renamed_session_keeps_its_label_after_the_named_project() -> None:
    """`SessionDisplayIdentity.rendered` puts the custom label last, after the generated
    part. Naming the project rewrites one field *inside* the generated part, so the label
    must survive it -- a rename the owner performed is the one part of this string they
    chose, and losing it to a cosmetic fix would be the worse trade."""
    record = _record(slug="opaque-existing", custom_label="nightly sweep")
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        trail = breadcrumb(app)

    assert "existing" in trail
    assert "opaque-existing" not in trail
    assert trail.index("nightly sweep") > trail.index("existing")
