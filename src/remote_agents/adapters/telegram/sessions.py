"""Deterministic, read-only running-session list presentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from remote_agents.domain.models import SessionRecord


@dataclass(frozen=True, slots=True)
class SessionListItem:
    identity: str
    state: str
    age: str


@dataclass(frozen=True, slots=True)
class SessionPage:
    items: tuple[SessionListItem, ...]
    page: int
    page_count: int
    empty: bool


def render_session_page(
    records: tuple[SessionRecord, ...],
    *,
    page: int,
    page_size: int,
    now: datetime | None = None,
) -> SessionPage:
    """Present newest sessions first with stable generated identities and bounded pages."""

    if page_size < 1:
        raise ValueError("session page size must be positive")
    ordered = tuple(
        sorted(
            records, key=lambda record: (record.created_at, str(record.session_id)), reverse=True
        )
    )
    page_count = max(1, (len(ordered) + page_size - 1) // page_size)
    index = min(max(page, 0), page_count - 1)
    visible = ordered[index * page_size : (index + 1) * page_size]
    current = datetime.now(UTC) if now is None else now
    return SessionPage(
        tuple(
            SessionListItem(item.display.rendered, str(item.state), _age(current, item.created_at))
            for item in visible
        ),
        index,
        page_count,
        not ordered,
    )


def _age(now: datetime, created_at: datetime) -> str:
    minutes = max(0, int((now - created_at).total_seconds() // 60))
    return f"{minutes}m"
