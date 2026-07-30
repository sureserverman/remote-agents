from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from remote_agents.adapters.telegram.sessions import render_session_page
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def record(number: int, sequence: int, label: str | None = None) -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=number)),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "Claude", "regular", sequence, label),
        SessionState.RUNNING,
        datetime(2026, 7, 31, tzinfo=UTC) - timedelta(minutes=number),
    )


def test_duplicate_sessions_remain_distinguishable_and_ordered_with_pagination() -> None:
    page = render_session_page(
        (record(2, 2, "draft"), record(1, 1, "draft")),
        page=0,
        page_size=1,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert page.page_count == 2
    assert page.items[0].identity == "opaque-editor · Claude · regular · #1 · draft"
    assert page.items[0].age == "1m"
    assert page.items[0].state == "running"


def test_empty_session_list_has_no_rows() -> None:
    page = render_session_page((), page=9, page_size=20)

    assert page.empty is True
    assert page.items == ()
