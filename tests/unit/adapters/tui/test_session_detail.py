"""Selecting a session opens a detail view that explains what it is."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import explain_state
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
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
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_EXISTING,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_EXISTING,),
    )


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


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
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert explain_state(state) in status


async def test_every_state_is_either_reachable_in_detail_or_deliberately_filtered() -> None:
    """Guards the exclusion above from silently growing to hide an unhandled state."""
    assert set(_REACHABLE) | {SessionState.ENDED} == set(SessionState)


async def test_an_ended_session_has_no_detail_to_open() -> None:
    """Filtered from the list, so selecting it can only mean it ended a moment ago."""
    record = _record(SessionState.ENDED)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert "no longer available" in status.casefold()


async def test_detail_names_the_session_and_its_state() -> None:
    record = _record(SessionState.PRESERVED)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert record.display.rendered in status
    assert "preserved" in status


async def test_escape_returns_from_detail_to_the_list() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        assert app._step is Step.SESSION_DETAIL
        await app.action_back()
        await pilot.pause()
        step = app._step

    assert step is Step.SESSIONS


async def test_a_session_that_vanished_between_list_and_detail_does_not_raise() -> None:
    """The store is shared; a session can be stopped elsewhere while this list is open."""
    listed = _record()
    launcher = _Listing((listed,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        launcher.records = ()
        await app._show_detail(str(listed.session_id))
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
        step = app._step
        status = _status(app)

    assert step is Step.SESSION_DETAIL
    assert record.display.rendered in status
