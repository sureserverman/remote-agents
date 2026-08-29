"""Provider activity evidence determines whether a pane is watched for quiet."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remote_agents.application.activity import PaneQuietWatcher, observe_codex_action_required
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind


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


async def test_a_reported_record_does_not_suppress_a_quiet_only_watch() -> None:
    record = _running_record("opencode")
    captures = iter(("start", "working", "working", "working", "working"))

    async def capture(session_id: SessionId) -> str:
        assert session_id == record.session_id
        return next(captures)

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2)

    assert await watcher.poll() == ()
    assert await watcher.poll() == ()
    watcher.mark_reported((str(record.session_id),))
    assert await watcher.poll() == ()
    (quiet,) = await watcher.poll()
    assert quiet.session_id == str(record.session_id)


def test_codex_action_required_is_a_rising_edge_not_pane_text() -> None:
    """The only title-derived state retained is whether the exact marker is present."""
    moment = datetime(2026, 8, 28, tzinfo=UTC)

    active, first = observe_codex_action_required(
        False,
        session_id="codex-title-session",
        title="[ ! ] Action Required | multitor",
        now=moment,
    )
    repeated, duplicate = observe_codex_action_required(
        active,
        session_id="codex-title-session",
        title="[ ! ] Action Required | multitor",
        now=moment,
    )
    cleared, after_clear = observe_codex_action_required(
        repeated,
        session_id="codex-title-session",
        title="multitor",
        now=moment,
    )

    assert active is True
    assert first is not None
    assert first.kind is ActivityKind.NEEDS_ANSWER
    assert first.confidence is ActivityConfidence.INFERRED
    assert first.detail is None
    assert repeated is True
    assert duplicate is None
    assert cleared is False
    assert after_clear is None


async def test_only_codex_titles_can_infer_an_action_required_notification() -> None:
    codex = _running_record("codex")
    other = _running_record("opencode")

    async def capture(session_id: SessionId) -> str:
        return "working"

    titles = iter(("multitor", "[ ! ] Action Required | multitor"))

    async def action_required(session_id: SessionId) -> str:
        return next(titles)

    class _TwoSessionStore:
        async def list(self, states: object) -> tuple[SessionRecord, ...]:
            assert states == (SessionState.RUNNING,)
            return (codex, other)

    watcher = PaneQuietWatcher(_TwoSessionStore(), capture, quiet_polls=2, title=action_required)

    assert await watcher.poll() == ()
    (activity,) = await watcher.poll()

    assert activity.session_id == str(codex.session_id)
    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.INFERRED


async def test_existing_action_required_title_is_a_restart_baseline_not_a_duplicate() -> None:
    record = _running_record("codex")

    async def capture(session_id: SessionId) -> str:
        return "working"

    async def action_required(session_id: SessionId) -> str:
        return "[ ! ] Action Required | multitor"

    first = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2, title=action_required)
    restarted = PaneQuietWatcher(
        _RunningStore(record), capture, quiet_polls=2, title=action_required
    )

    assert await first.poll() == ()
    assert await restarted.poll() == ()


async def test_first_title_read_failure_keeps_recovered_marker_as_restart_baseline() -> None:
    record = _running_record("codex")
    titles: list[str | Exception] = [
        RuntimeError("tmux unavailable"),
        "[ ! ] Action Required | multitor",
    ]

    async def capture(session_id: SessionId) -> str:
        return "working"

    async def action_required(session_id: SessionId) -> str:
        title = titles.pop(0)
        if isinstance(title, Exception):
            raise title
        return title

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2, title=action_required)

    assert await watcher.poll() == ()
    assert await watcher.poll() == ()


async def test_title_read_failure_reenables_quiet_without_reemitting_the_open_prompt() -> None:
    record = _running_record("codex")
    titles: list[str | Exception] = [
        "multitor",
        "[ ! ] Action Required | multitor",
        RuntimeError("tmux unavailable"),
        RuntimeError("tmux unavailable"),
    ]
    captures = iter(("start", "working", "working", "working"))

    async def capture(session_id: SessionId) -> str:
        return next(captures)

    async def action_required(session_id: SessionId) -> str:
        title = titles.pop(0)
        if isinstance(title, Exception):
            raise title
        return title

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2, title=action_required)

    assert await watcher.poll() == ()
    (needs_answer,) = await watcher.poll()
    assert needs_answer.kind is ActivityKind.NEEDS_ANSWER
    assert await watcher.poll() == ()
    (quiet,) = await watcher.poll()
    assert quiet.kind is ActivityKind.QUIET


async def test_title_recovery_rearms_the_generic_notice_after_an_unavailable_period() -> None:
    record = _running_record("codex")
    titles: list[str | Exception] = [
        "multitor",
        "[ ! ] Action Required | multitor",
        RuntimeError("tmux unavailable"),
        "[ ! ] Action Required | multitor",
    ]

    async def capture(session_id: SessionId) -> str:
        return "working"

    async def action_required(session_id: SessionId) -> str:
        title = titles.pop(0)
        if isinstance(title, Exception):
            raise title
        return title

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2, title=action_required)

    assert await watcher.poll() == ()
    assert (await watcher.poll())[0].kind is ActivityKind.NEEDS_ANSWER
    assert await watcher.poll() == ()
    (recovered,) = await watcher.poll()
    assert recovered.kind is ActivityKind.NEEDS_ANSWER


async def test_reported_permission_wins_over_the_same_title_edge() -> None:
    record = _running_record("codex")
    titles = iter(
        (
            "[ ! ] Action Required | multitor",
            "multitor",
            "[ ! ] Action Required | multitor",
        )
    )

    async def capture(session_id: SessionId) -> str:
        return "working"

    async def action_required(session_id: SessionId) -> str:
        return next(titles)

    watcher = PaneQuietWatcher(_RunningStore(record), capture, quiet_polls=2, title=action_required)
    watcher.mark_needs_answer_reported((str(record.session_id),))

    assert await watcher.poll() == ()
    assert await watcher.poll() == ()
    (activity,) = await watcher.poll()

    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.INFERRED
