"""Provider activity evidence determines whether a pane is watched for quiet."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remote_agents.application.activity import PaneQuietWatcher
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


class _RunningStore:
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list(self, states: object) -> tuple[SessionRecord, ...]:
        assert states == (SessionState.RUNNING,)
        return (self.record,)


def _running_record(profile_id: str) -> SessionRecord:
    session_id = SessionId("source-policy-session")
    return SessionRecord(
        session_id,
        ProjectId("source-policy-project"),
        ProfileId(profile_id),
        SessionDisplayIdentity("source-policy-project", profile_id, "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 27, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("profile_id", "expected_captures"),
    [("claude", 0), ("opencode", 1), ("codex", 1)],
)
async def test_activity_source_policy_selects_the_right_quiet_watchers(
    profile_id: str, expected_captures: int
) -> None:
    record = _running_record(profile_id)
    captures = 0

    async def capture(session_id: SessionId) -> str:
        nonlocal captures
        assert session_id == record.session_id
        captures += 1
        return "waiting"

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2)

    assert await watcher.poll() == ()
    assert captures == expected_captures


async def test_a_reported_hybrid_event_suppresses_only_its_current_quiet_spell() -> None:
    record = _running_record("codex")
    captures = iter(
        (
            "start",
            "working",
            "working",
            "working",
            "working again",
            "working again",
            "working again",
        )
    )

    async def capture(session_id: SessionId) -> str:
        assert session_id == record.session_id
        return next(captures)

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2)

    assert await watcher.poll() == ()  # baseline
    assert await watcher.poll() == ()  # observed change arms this spell
    watcher.mark_reported((str(record.session_id),))
    assert await watcher.poll() == ()
    assert await watcher.poll() == ()

    assert await watcher.poll() == ()  # a new pane change re-arms quiet inference
    assert await watcher.poll() == ()
    (quiet,) = await watcher.poll()
    assert quiet.session_id == str(record.session_id)
