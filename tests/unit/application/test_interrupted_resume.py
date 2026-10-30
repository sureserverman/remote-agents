"""Which sessions a restart that lost the tmux server resumes, and why the rest are skipped."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.interrupted_resume import (
    RestartSnapshot,
    ResumeSkip,
    SessionAtRestart,
    plan_resumes,
)
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.rollover_store import Rollover

PROJECT = ProjectId("remote-agents")
CLAUDE = ProfileId("claude")
CODEX = ProfileId("codex")
CURSOR = ProfileId("cursor")
CREATED = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
REBOOTED = CREATED + timedelta(hours=3)


def _record(
    *,
    state: SessionState = SessionState.RUNNING,
    profile: ProfileId = CLAUDE,
    created: datetime = CREATED,
    resume_source_id: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        PROJECT,
        profile,
        SessionDisplayIdentity(str(PROJECT), str(profile), "regular", 1, None),
        state,
        created,
        None if resume_source_id is None else profile,
        resume_source_id,
    )


def _snapshot(
    *sessions: SessionAtRestart,
    server_started_at: datetime | None = None,
    server_known: bool = True,
    markers: tuple[SessionRecord, ...] = (),
    marks: dict[SessionRecord, str] | None = None,
    rollovers: tuple[Rollover, ...] = (),
    resumable: frozenset[ProfileId] = frozenset({CLAUDE, CODEX}),
) -> RestartSnapshot:
    return RestartSnapshot(
        sessions=sessions,
        server_started_at=server_started_at,
        server_known=server_known,
        turn_marker_sessions=frozenset(str(record.session_id) for record in markers),
        conversation_marks={str(record.session_id): mark for record, mark in (marks or {}).items()},
        rollovers=rollovers,
        resumable_profiles=resumable,
    )


def _lost(record: SessionRecord) -> SessionAtRestart:
    return SessionAtRestart(record, pane_listed=False)


def _live(record: SessionRecord) -> SessionAtRestart:
    return SessionAtRestart(record, pane_listed=True)


def _rollover(
    predecessor: SessionRecord, state: RolloverState, successor: SessionRecord | None = None
) -> Rollover:
    return Rollover(
        id="r-1",
        handoff_id="h-0123456789abcdef0123",
        predecessor_session_id=predecessor.session_id,
        successor_session_id=None if successor is None else successor.session_id,
        project_id=PROJECT,
        profile_id=predecessor.profile_id,
        reason="workflow",
        plan=None,
        state=state,
        failure_code=None,
        failure_detail=None,
        requested_at=CREATED,
        updated_at=CREATED,
    )


def test_a_session_lost_with_an_absent_server_resumes_in_its_marked_conversation() -> None:
    record = _record()

    (decision,) = plan_resumes(_snapshot(_lost(record), marks={record: "conv-a"}))

    assert decision.record == record
    assert decision.skip is None
    assert decision.conversation_id == ProviderConversationId("conv-a")
    assert decision.nudge is False


def test_a_server_started_after_the_record_counts_as_lost() -> None:
    """The console can start a fresh server before `serve`: present, and every old pane gone."""
    record = _record()

    (decision,) = plan_resumes(
        _snapshot(_lost(record), server_started_at=REBOOTED, marks={record: "conv-a"})
    )

    assert decision.skip is None


@pytest.mark.parametrize(
    "started", [CREATED - timedelta(hours=1), CREATED, CREATED + timedelta(seconds=1)]
)
def test_nothing_is_resumed_when_the_server_was_present(started: datetime) -> None:
    """A service-only restart leaves panes alive; one pane killed under a live server is not ours.

    One second of slack: tmux reports its start time in whole seconds, and the first launch on
    a host starts the server itself, a moment after its record was written.
    """
    killed = _record()
    alive = _record()

    assert (
        plan_resumes(
            _snapshot(
                _lost(killed),
                _live(alive),
                server_started_at=started,
                marks={killed: "conv-a", alive: "conv-b"},
            )
        )
        == ()
    )


def test_a_pane_still_listed_is_never_resumed() -> None:
    record = _record()

    assert plan_resumes(_snapshot(_live(record), marks={record: "conv-a"})) == ()


def test_an_unknown_server_resumes_nothing() -> None:
    """`server_started_at` raised: no reading of the server, so no unattended launch."""
    record = _record()

    assert plan_resumes(_snapshot(_lost(record), server_known=False, marks={record: "c"})) == ()


@pytest.mark.parametrize(
    "state",
    [
        SessionState.PRESERVED,
        SessionState.STOP_REQUESTED,
        SessionState.UNTRUSTED,
        SessionState.STARTING,
        SessionState.ORPHANED,
        SessionState.ENDED,
    ],
)
def test_no_other_state_is_resumed_even_with_a_mark(state: SessionState) -> None:
    """DEC-020: an ORPHANED record is cleared by acting, never by an unattended relaunch."""
    record = _record(state=state)

    assert plan_resumes(_snapshot(_lost(record), marks={record: "conv-a"})) == ()


def test_a_failed_record_that_took_a_turn_is_resumed_once_after_a_lost_server() -> None:
    """The incident: recorded failed at launch + 20 s, worked for three hours, lost at a reboot."""
    record = _record(state=SessionState.FAILED)

    (decision,) = plan_resumes(_snapshot(_lost(record), marks={record: "conv-a"}))

    assert decision.record == record
    assert decision.conversation_id == ProviderConversationId("conv-a")
    assert decision.skip is None


def test_a_failed_record_that_never_took_a_turn_is_not_considered() -> None:
    """Absent rather than skipped: a skip is named in the notice as work that did not come back."""
    record = _record(state=SessionState.FAILED)

    assert plan_resumes(_snapshot(_lost(record))) == ()


def test_a_failed_resume_with_only_its_source_is_not_considered() -> None:
    """A resume that failed never proved a turn, so its source says nothing about this record."""
    record = _record(state=SessionState.FAILED, resume_source_id="conv-a")

    assert plan_resumes(_snapshot(_lost(record))) == ()


def test_a_failed_record_whose_mark_is_not_an_identifier_is_not_considered() -> None:
    """The source must not stand in for a mark that cannot be read as one."""
    record = _record(state=SessionState.FAILED, resume_source_id="conv-a")

    assert plan_resumes(_snapshot(_lost(record), marks={record: "not an id"})) == ()


def test_a_failed_record_with_a_mark_and_a_listed_pane_is_not_resumed() -> None:
    record = _record(state=SessionState.FAILED)

    assert plan_resumes(_snapshot(_live(record), marks={record: "conv-a"})) == ()


def test_a_failed_record_is_not_resumed_when_the_server_outlived_it() -> None:
    """One pane killed under a live server is the owner's doing, for a failed row as for any."""
    record = _record(state=SessionState.FAILED)
    snapshot = _snapshot(
        _lost(record), server_started_at=CREATED - timedelta(days=1), marks={record: "conv-a"}
    )

    assert plan_resumes(snapshot) == ()


def test_a_failed_record_whose_conversation_another_live_record_holds_is_already_open() -> None:
    record = _record(state=SessionState.FAILED)
    other = _record(created=CREATED + timedelta(hours=1))

    decisions = plan_resumes(
        _snapshot(_lost(record), _live(other), marks={record: "conv-a", other: "conv-a"})
    )

    assert [(item.record, item.skip) for item in decisions] == [(record, ResumeSkip.ALREADY_OPEN)]


def test_the_conversation_mark_wins_over_the_resume_source() -> None:
    record = _record(resume_source_id="older")

    (decision,) = plan_resumes(_snapshot(_lost(record), marks={record: "newer"}))

    assert decision.conversation_id == ProviderConversationId("newer")


def test_without_a_mark_the_resume_source_is_the_conversation() -> None:
    record = _record(resume_source_id="from-resume")

    (decision,) = plan_resumes(_snapshot(_lost(record)))

    assert decision.skip is None
    assert decision.conversation_id == ProviderConversationId("from-resume")


def test_without_a_mark_or_a_resume_source_the_session_is_skipped() -> None:
    record = _record()

    (decision,) = plan_resumes(_snapshot(_lost(record), markers=(record,)))

    assert decision.skip is ResumeSkip.NO_CONVERSATION
    assert decision.conversation_id is None
    assert decision.nudge is False


def test_a_mark_that_is_not_an_identifier_falls_to_the_resume_source() -> None:
    """DEC-067: identifiers only. A mark with a space is not one, whoever wrote it."""
    record = _record(resume_source_id="from-resume")

    (decision,) = plan_resumes(_snapshot(_lost(record), marks={record: "two words"}))

    assert decision.conversation_id == ProviderConversationId("from-resume")


def test_a_profile_without_resume_is_skipped_as_not_resumable() -> None:
    record = _record(profile=CURSOR)

    (decision,) = plan_resumes(_snapshot(_lost(record), marks={record: "conv-a"}))

    assert decision.skip is ResumeSkip.NOT_RESUMABLE


@pytest.mark.parametrize(
    "state",
    [
        RolloverState.SUCCESSOR_ACCEPTED,
        RolloverState.PREDECESSOR_STOPPING,
        RolloverState.COMPLETED,
        RolloverState.STOP_FAILED,
    ],
)
def test_rollover_predecessor_handed_off_is_skipped(state: RolloverState) -> None:
    predecessor = _record()
    successor = _record(created=CREATED + timedelta(minutes=5))

    decisions = plan_resumes(
        _snapshot(
            _lost(predecessor),
            _lost(successor),
            marks={predecessor: "conv-old", successor: "conv-new"},
            rollovers=(_rollover(predecessor, state, successor),),
        )
    )

    by_record = {decision.record: decision for decision in decisions}
    assert by_record[predecessor].skip is ResumeSkip.HANDED_OFF
    assert by_record[successor].skip is None
    assert by_record[successor].conversation_id == ProviderConversationId("conv-new")


@pytest.mark.parametrize(
    "state",
    [
        RolloverState.REQUESTED,
        RolloverState.HANDOFF_READY,
        RolloverState.SUCCESSOR_STARTING,
        RolloverState.ADOPTING,
        RolloverState.FAILED,
        RolloverState.CANCELLED,
    ],
)
def test_rollover_predecessor_not_yet_handed_off_is_resumed(state: RolloverState) -> None:
    """Before acceptance the predecessor still holds the work, so it comes back."""
    predecessor = _record()

    (decision,) = plan_resumes(
        _snapshot(
            _lost(predecessor),
            marks={predecessor: "conv-old"},
            rollovers=(_rollover(predecessor, state),),
        )
    )

    assert decision.skip is None


@pytest.mark.parametrize("state", [RolloverState.SUCCESSOR_STARTING, RolloverState.ADOPTING])
def test_a_successor_whose_rollover_was_not_yet_accepted_is_not_resumed(
    state: RolloverState,
) -> None:
    """Cut off before acceptance, the predecessor still holds the work (DEC-115): it comes back,
    and the successor does not, so one session -- not two -- carries the plan. The rollover pass
    then fails the rollover and says so."""
    predecessor = _record()
    successor = _record(created=CREATED + timedelta(minutes=5))

    decisions = plan_resumes(
        _snapshot(
            _lost(predecessor),
            _lost(successor),
            marks={predecessor: "conv-old", successor: "conv-new"},
            rollovers=(_rollover(predecessor, state, successor),),
        )
    )

    by_record = {decision.record: decision for decision in decisions}
    assert by_record[predecessor].skip is None
    assert by_record[successor].skip is ResumeSkip.NOT_ADOPTED
    assert not by_record[successor].nudge


def test_a_conversation_another_live_record_holds_is_already_open() -> None:
    """A live pane resumed the same conversation (say, from the console) after a fresh server."""
    lost = _record()
    holder = _record(created=REBOOTED + timedelta(minutes=1), resume_source_id="conv-a")

    (decision,) = plan_resumes(
        _snapshot(_lost(lost), _live(holder), server_started_at=REBOOTED, marks={lost: "conv-a"})
    )

    assert decision.record == lost
    assert decision.skip is ResumeSkip.ALREADY_OPEN


def test_two_lost_records_on_one_conversation_resume_it_once() -> None:
    """Codex marks inherit DEC-126's roster heuristic, so two sessions can name one thread."""
    first = _record(profile=CODEX)
    second = _record(profile=CODEX, created=CREATED + timedelta(minutes=1))

    decisions = plan_resumes(
        _snapshot(_lost(second), _lost(first), marks={first: "thread", second: "thread"})
    )

    assert [(d.record, d.skip) for d in decisions] == [
        (first, None),
        (second, ResumeSkip.ALREADY_OPEN),
    ]


def test_only_a_session_with_a_turn_marker_is_nudged() -> None:
    """Owner ruling: nudge only the sessions that were mid-turn."""
    marked = _record()
    idle = _record(created=CREATED + timedelta(minutes=1))

    decisions = plan_resumes(
        _snapshot(_lost(marked), _lost(idle), markers=(marked,), marks={marked: "a", idle: "b"})
    )

    assert [(d.record, d.nudge) for d in decisions] == [(marked, True), (idle, False)]


def test_a_skipped_session_is_never_nudged() -> None:
    record = _record(profile=CURSOR)

    (decision,) = plan_resumes(_snapshot(_lost(record), markers=(record,), marks={record: "a"}))

    assert decision.skip is ResumeSkip.NOT_RESUMABLE
    assert decision.nudge is False


def test_there_is_no_age_limit() -> None:
    """Owner ruling: a session lost a year ago is resumed like one lost a minute ago."""
    record = _record(created=CREATED - timedelta(days=365))

    (decision,) = plan_resumes(
        _snapshot(_lost(record), server_started_at=REBOOTED, marks={record: "conv-a"})
    )

    assert decision.skip is None


def test_decisions_come_oldest_first_and_say_nothing_twice() -> None:
    old = _record()
    new = _record(created=CREATED + timedelta(minutes=2))

    decisions = plan_resumes(_snapshot(_lost(new), _lost(old), marks={old: "a", new: "b"}))

    assert [decision.record for decision in decisions] == [old, new]
    assert len({decision.record.session_id for decision in decisions}) == 2


def test_the_snapshot_is_not_mutated() -> None:
    record = _record()
    snapshot = _snapshot(_lost(record), marks={record: "conv-a"})
    before = replace(snapshot)

    plan_resumes(snapshot)

    assert snapshot == before


def test_a_session_an_earlier_start_left_resuming_counts_whatever_its_state() -> None:
    """Reconcile ended it after the earlier start; the intent is what says it was interrupted."""
    record = _record(state=SessionState.ENDED)
    snapshot = replace(
        _snapshot(_lost(record), server_started_at=CREATED, marks={record: "conv-a"}),
        pending=frozenset({str(record.session_id)}),
    )

    (decision,) = plan_resumes(snapshot)

    assert decision.skip is None


def test_a_pending_session_whose_pane_is_listed_is_left_alone() -> None:
    record = _record()
    snapshot = replace(
        _snapshot(_live(record), marks={record: "conv-a"}),
        pending=frozenset({str(record.session_id)}),
    )

    assert plan_resumes(snapshot) == ()


def test_a_pending_session_whose_conversation_was_opened_since_is_superseded() -> None:
    """The owner resumed it by hand after a deferral, and finished: never relaunched."""
    pending = _record(state=SessionState.ENDED)
    later = _record(
        state=SessionState.ENDED, created=CREATED + timedelta(days=2), resume_source_id="conv-a"
    )
    snapshot = replace(
        _snapshot(_lost(pending), _lost(later), marks={pending: "conv-a"}),
        pending=frozenset({str(pending.session_id)}),
    )

    (decision,) = plan_resumes(snapshot)

    assert decision.record == pending
    assert decision.skip is ResumeSkip.SUPERSEDED


def test_a_pending_session_with_no_newer_holder_is_still_resumed() -> None:
    pending = _record(state=SessionState.ENDED)
    older = _record(
        state=SessionState.ENDED, created=CREATED - timedelta(days=2), resume_source_id="conv-a"
    )
    snapshot = replace(
        _snapshot(_lost(pending), _lost(older), marks={pending: "conv-a"}),
        pending=frozenset({str(pending.session_id)}),
    )

    (decision,) = plan_resumes(snapshot)

    assert decision.skip is None
