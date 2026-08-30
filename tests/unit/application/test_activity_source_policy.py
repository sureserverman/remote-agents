"""Provider activity evidence determines which panes are watched, and for what."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remote_agents.application.activity import (
    CodexApprovalWatcher,
    observe_codex_action_required,
)
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
    ("profile_id", "expected_title_reads"),
    [("claude", 0), ("opencode", 0), ("codex", 1)],
)
async def test_only_the_hybrid_profile_costs_a_pane_read(
    profile_id: str, expected_title_reads: int
) -> None:
    """`opencode` moved from 1 to 0 when the digest watch went, and that is the point.

    The old predicate was `is not HOOK_EXCLUSIVE`, which swept in every profile that had no
    hooks -- correct while a pane digest was their fallback. Codex is now the only provider
    anything here can observe, so a surviving `opencode` branch would spend one tmux round
    trip per session per pass reading a title that provider never sets.
    """
    record = _running_record(profile_id)
    reads = 0

    async def title(session_id: SessionId) -> str:
        nonlocal reads
        assert session_id == record.session_id
        reads += 1
        return "waiting"

    watcher = CodexApprovalWatcher(_RunningStore(record), title)

    assert await watcher.poll() == ()
    assert reads == expected_title_reads


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

    titles = iter(("multitor", "[ ! ] Action Required | multitor"))

    async def action_required(session_id: SessionId) -> str:
        return next(titles)

    class _TwoSessionStore:
        async def list(self, states: object) -> tuple[SessionRecord, ...]:
            assert states == (SessionState.RUNNING,)
            return (codex, other)

    watcher = CodexApprovalWatcher(_TwoSessionStore(), action_required)

    assert await watcher.poll() == ()
    (activity,) = await watcher.poll()

    assert activity.session_id == str(codex.session_id)
    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.INFERRED


async def test_existing_action_required_title_is_a_restart_baseline_not_a_duplicate() -> None:
    record = _running_record("codex")

    async def action_required(session_id: SessionId) -> str:
        return "[ ! ] Action Required | multitor"

    first = CodexApprovalWatcher(_RunningStore(record), action_required)
    restarted = CodexApprovalWatcher(_RunningStore(record), action_required)

    assert await first.poll() == ()
    assert await restarted.poll() == ()


async def test_first_title_read_failure_keeps_recovered_marker_as_restart_baseline() -> None:
    record = _running_record("codex")
    titles: list[str | Exception] = [
        RuntimeError("tmux unavailable"),
        "[ ! ] Action Required | multitor",
    ]

    async def action_required(session_id: SessionId) -> str:
        title = titles.pop(0)
        if isinstance(title, Exception):
            raise title
        return title

    watcher = CodexApprovalWatcher(_RunningStore(record), action_required)

    assert await watcher.poll() == ()
    assert await watcher.poll() == ()


async def test_title_read_failure_clears_the_marker_without_reemitting_the_open_prompt() -> None:
    """An unreadable title must not latch a stale positive marker, and must not re-announce.

    The clearing half is what stops a marker read once and then never re-read from silencing
    every later prompt for the life of the process. The silence half is the other side of the
    same edge: an unavailable title is not evidence that a *new* prompt opened, so nothing is
    emitted while it cannot be read. This used to end by proving the pane-digest fallback
    stayed eligible through the outage; that fallback was retired on 2026-08-30, and what the
    test is really pinning -- the edge state DEC-063 keeps -- is unchanged.
    """
    record = _running_record("codex")
    titles: list[str | Exception] = [
        "multitor",
        "[ ! ] Action Required | multitor",
        RuntimeError("tmux unavailable"),
        RuntimeError("tmux unavailable"),
    ]

    async def action_required(session_id: SessionId) -> str:
        title = titles.pop(0)
        if isinstance(title, Exception):
            raise title
        return title

    watcher = CodexApprovalWatcher(_RunningStore(record), action_required)

    assert await watcher.poll() == ()
    (needs_answer,) = await watcher.poll()
    assert needs_answer.kind is ActivityKind.NEEDS_ANSWER
    assert await watcher.poll() == ()
    assert await watcher.poll() == ()


async def test_title_recovery_rearms_the_generic_notice_after_an_unavailable_period() -> None:
    record = _running_record("codex")
    titles: list[str | Exception] = [
        "multitor",
        "[ ! ] Action Required | multitor",
        RuntimeError("tmux unavailable"),
        "[ ! ] Action Required | multitor",
    ]

    async def action_required(session_id: SessionId) -> str:
        title = titles.pop(0)
        if isinstance(title, Exception):
            raise title
        return title

    watcher = CodexApprovalWatcher(_RunningStore(record), action_required)

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

    async def action_required(session_id: SessionId) -> str:
        return next(titles)

    watcher = CodexApprovalWatcher(_RunningStore(record), action_required)
    watcher.mark_needs_answer_reported((str(record.session_id),))

    assert await watcher.poll() == ()
    assert await watcher.poll() == ()
    (activity,) = await watcher.poll()

    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.INFERRED
