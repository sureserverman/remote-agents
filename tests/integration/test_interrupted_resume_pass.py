"""The restart resume pass over the real stores: each interrupted session is acted on once.

Task 2.3 of the restart-resume plan. The pass writes its intent before it launches, finishes
with `resumed:<id>` or `skipped:<reason>`, and never acts on a decided row again (DEC-114). A
`resuming` intent left by a busy refusal is tried again on the next start, and the
`restart:<session>` key is what keeps that from launching twice.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.interrupted_resumes import SQLiteInterruptedResumes
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import UNATTENDED_LAUNCH_WAIT_SECONDS, ResumeCommand
from remote_agents.application.errors import MutationsDraining
from remote_agents.application.interrupted_resume import (
    DEFERRED,
    RESUMED_LABEL,
    InterruptedResumePass,
    RestartSnapshot,
    SessionAtRestart,
)
from remote_agents.application.limit_resume import NUDGE
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.schedules import RETRY_SECONDS, STARTUP_PATIENCE, STARTUP_RETRIES
from remote_agents.application.services import ResumeOutcome, SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy
from remote_agents.ports.interrupted_resumes import RESUMING
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason
from remote_agents.ports.worktree_store import Worktree

PROJECT = ProjectId("remote-agents")
CLAUDE = ProfileId("claude")
CREATED = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
NOW = CREATED + timedelta(hours=3)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.sessions = SQLiteSessionStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        self.outcomes = SQLiteInterruptedResumes(self.connection)
        self.terminal = FakeTerminal()
        self.terminal.server_started = None
        self.locks = SessionLocks()
        self.service = SessionService(
            self.sessions, self.terminal, locks=self.locks, worktrees=self.worktrees
        )
        self.commands: list[ResumeCommand] = []
        self.marks: dict[str, str] = {}
        self.mid_turn: set[str] = set()
        self.clock = NOW
        self.sleeps: list[float] = []

    async def running(self, mark: str | None = "conv", *, created: datetime = CREATED):
        record = SessionRecord(
            SessionId.new(),
            PROJECT,
            CLAUDE,
            SessionDisplayIdentity(str(PROJECT), str(CLAUDE), "regular", 1),
            SessionState.RUNNING,
            created,
        )
        await self.sessions.save(record)
        if mark is not None:
            self.marks[str(record.session_id)] = f"{mark}-{record.session_id}"
        return record

    async def snapshot(self, *records: SessionRecord) -> RestartSnapshot:
        decided = await self.outcomes.outcomes()
        return RestartSnapshot(
            sessions=tuple(SessionAtRestart(record, pane_listed=False) for record in records),
            server_started_at=None,
            server_known=True,
            turn_marker_sessions=frozenset(self.mid_turn),
            conversation_marks=dict(self.marks),
            rollovers=(),
            resumable_profiles=frozenset({CLAUDE}),
            pending=frozenset(
                session for session, outcome in decided.items() if outcome == RESUMING
            ),
        )

    async def end_as_reconcile_does(self, *records: SessionRecord) -> None:
        for record in records:
            await self.sessions.record_event(
                record.session_id, LifecycleEvent.RECONCILED_TERMINAL_MISSING
            )

    def run_pass(self, resume=None) -> InterruptedResumePass:
        async def recorded(command: ResumeCommand):
            self.commands.append(command)
            return await self.service.resume(command)

        return InterruptedResumePass(
            self.outcomes,
            resume=resume or recorded,
            folder_of=self.worktrees.folder_for,
            key_claimed=self.sessions.idempotency_key_claimed,
            send=self.terminal.send_prompt,
            sleep=self.sleep,
            stopping=lambda: self.locks.draining,
            now=lambda: self.clock,
        )

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += timedelta(seconds=seconds)


@pytest.fixture
def rig(tmp_path: Path):
    rig = Rig(tmp_path)
    yield rig
    rig.connection.close()


async def test_two_running_records_resume_to_two_new_running_records(rig: Rig) -> None:
    first = await rig.running()
    second = await rig.running(created=CREATED + timedelta(minutes=1))
    snapshot = await rig.snapshot(first, second)
    await rig.end_as_reconcile_does(first, second)

    reports = await rig.run_pass().run(snapshot)

    assert [report.record for report in reports] == [first, second]
    resumed = [
        record for record in await rig.sessions.list() if record.state is SessionState.RUNNING
    ]
    assert len(resumed) == 2
    assert {record.resume_source_id for record in resumed} == {
        rig.marks[str(first.session_id)],
        rig.marks[str(second.session_id)],
    }
    assert all(record.display.custom_label == RESUMED_LABEL for record in resumed)
    outcomes = await rig.outcomes.outcomes()
    for report in reports:
        assert report.new_session_id is not None
        assert outcomes[str(report.record.session_id)] == f"resumed:{report.new_session_id}"


async def test_the_resume_is_keyed_and_waits_as_an_unattended_launch(rig: Rig) -> None:
    record = await rig.running()
    snapshot = await rig.snapshot(record)

    await rig.run_pass().run(snapshot)

    (command,) = rig.commands
    assert command.idempotency_key == f"restart:{record.session_id}"
    assert command.guard_wait == UNATTENDED_LAUNCH_WAIT_SECONDS
    assert command.label == RESUMED_LABEL
    assert command.conversation.provider_conversation_id.value == rig.marks[str(record.session_id)]


async def test_a_second_pass_over_the_same_snapshot_launches_nothing(rig: Rig) -> None:
    record = await rig.running()
    snapshot = await rig.snapshot(record)
    await rig.run_pass().run(snapshot)
    launched = len(rig.commands)

    again = await rig.run_pass().run(snapshot)

    assert len(rig.commands) == launched == 1
    assert again == ()


async def test_a_skip_is_recorded_once_and_launches_nothing(rig: Rig) -> None:
    record = await rig.running(mark=None)
    snapshot = await rig.snapshot(record)

    (report,) = await rig.run_pass().run(snapshot)
    again = await rig.run_pass().run(snapshot)

    assert report.outcome == "skipped:no-conversation"
    assert (await rig.outcomes.outcomes())[str(record.session_id)] == "skipped:no-conversation"
    assert rig.commands == [] and again == ()


async def test_a_pass_killed_after_the_intent_launches_nothing_on_restart(rig: Rig) -> None:
    """Killed inside the resume, its key claimed and no pane started: the next start must not
    launch a second time, so the key refuses it and the outcome reads `skipped:interrupted`."""
    record = await rig.running()
    snapshot = await rig.snapshot(record)
    await rig.end_as_reconcile_does(record)

    async def killed(command: ResumeCommand):
        assert await rig.sessions.claim_idempotency_key(command.idempotency_key)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await rig.run_pass(resume=killed).run(snapshot)
    assert (await rig.outcomes.outcomes())[str(record.session_id)] == RESUMING

    (report,) = await rig.run_pass().run(await rig.snapshot(await _get(rig, record)))

    assert report.outcome == "skipped:interrupted"
    assert [r for r in await rig.sessions.list() if r.state is SessionState.RUNNING] == []


@pytest.mark.parametrize("busy", [CodexRemoteControlBusy, MutationsDraining])
async def test_a_busy_refusal_leaves_the_intent_and_the_next_start_resumes(
    rig: Rig, busy: type[Exception]
) -> None:
    """Refused before the key was claimed: nothing started, so the next start tries again."""
    record = await rig.running()
    snapshot = await rig.snapshot(record)
    await rig.end_as_reconcile_does(record)

    async def refused(command: ResumeCommand):
        raise busy("busy")

    reports = await rig.run_pass(resume=refused).run(snapshot)

    # Draining stops the pass with nothing to report; the daemon guard defers this one only.
    assert [report.outcome for report in reports] == (
        [] if busy is MutationsDraining else [DEFERRED]
    )
    assert (await rig.outcomes.outcomes())[str(record.session_id)] == RESUMING

    (report,) = await rig.run_pass().run(await rig.snapshot(await _get(rig, record)))

    assert report.outcome.startswith("resumed:")
    assert len(rig.commands) == 1


async def test_a_worktree_record_resumes_in_its_worktree(rig: Rig, tmp_path: Path) -> None:
    record = await rig.running()
    folder = await _worktree(rig, record, tmp_path / "worktrees" / "one")
    snapshot = await rig.snapshot(record)
    await rig.end_as_reconcile_does(record)

    (report,) = await rig.run_pass().run(snapshot)

    assert report.new_session_id is not None
    bound = await rig.worktrees.folder_for(report.new_session_id)
    assert bound is not None and bound.path == folder


async def test_a_removed_worktree_is_skipped_and_never_the_main_checkout(
    rig: Rig, tmp_path: Path
) -> None:
    """DEC-123: a resumed session reuses its worktree, and a missing one is a skip."""
    record = await rig.running()
    folder = await _worktree(rig, record, tmp_path / "worktrees" / "gone")
    await rig.worktrees.mark_removed(folder, at=CREATED + timedelta(hours=1))
    snapshot = await rig.snapshot(record)

    (report,) = await rig.run_pass().run(snapshot)

    assert report.outcome == "skipped:worktree_removed"
    assert rig.commands == []


async def test_a_launch_error_after_the_claim_is_decided_and_never_tried_again(rig: Rig) -> None:
    record = await rig.running()
    snapshot = await rig.snapshot(record)

    async def broken(command: ResumeCommand):
        assert await rig.sessions.claim_idempotency_key(command.idempotency_key)
        raise RuntimeError("tmux said no")

    (report,) = await rig.run_pass(resume=broken).run(snapshot)
    again = await rig.run_pass().run(await rig.snapshot(record))

    assert report.outcome == "skipped:launch_error"
    assert again == () and rig.commands == []


async def _get(rig: Rig, record: SessionRecord) -> SessionRecord:
    current = await rig.sessions.get(record.session_id)
    assert current is not None
    return current


async def _worktree(rig: Rig, record: SessionRecord, path: Path) -> Path:
    path.mkdir(parents=True)
    await rig.worktrees.record(
        Worktree(
            path=path,
            project_id=PROJECT,
            branch="ra/one",
            base_branch="main",
            base_commit="a" * 40,
            copied={},
            created_at=CREATED,
            removed_at=None,
        ),
        bound_to=record.session_id,
    )
    return path


async def test_an_error_before_the_claim_leaves_the_intent_for_the_next_start(rig: Rig) -> None:
    """A locked database or a guard read failing at startup started nothing: try again later."""
    record = await rig.running()
    snapshot = await rig.snapshot(record)
    await rig.end_as_reconcile_does(record)

    async def locked(command: ResumeCommand):
        raise RuntimeError("database is locked")

    (report,) = await rig.run_pass(resume=locked).run(snapshot)

    assert report.outcome == DEFERRED
    assert (await rig.outcomes.outcomes())[str(record.session_id)] == RESUMING
    assert not await rig.sessions.idempotency_key_claimed(f"restart:{record.session_id}")

    (again,) = await rig.run_pass().run(await rig.snapshot(await _get(rig, record)))

    assert again.outcome.startswith("resumed:")


async def test_a_draining_service_refuses_before_the_claim_and_the_pass_stops(rig: Rig) -> None:
    """The real `SessionService.resume`: draining refuses at the gate, before any key is claimed."""
    first = await rig.running()
    second = await rig.running(created=CREATED + timedelta(minutes=1))
    snapshot = await rig.snapshot(first, second)
    await rig.locks.drain()

    reports = await rig.run_pass().run(snapshot)

    assert reports == ()
    outcomes = await rig.outcomes.outcomes()
    # Both intents were written before the first launch, so neither is lost to the next start.
    assert outcomes == {str(first.session_id): RESUMING, str(second.session_id): RESUMING}
    assert not await rig.sessions.idempotency_key_claimed(f"restart:{first.session_id}")


async def test_draining_mid_pass_keeps_what_was_already_resumed(rig: Rig) -> None:
    first = await rig.running()
    second = await rig.running(created=CREATED + timedelta(minutes=1))
    snapshot = await rig.snapshot(first, second)

    async def drains_after_one(command: ResumeCommand):
        if rig.commands:
            raise MutationsDraining("mutations are draining")
        rig.commands.append(command)
        return await rig.service.resume(command)

    reports = await rig.run_pass(resume=drains_after_one).run(snapshot)

    assert [report.record for report in reports] == [first]
    assert reports[0].outcome.startswith("resumed:")
    assert (await rig.outcomes.outcomes())[str(second.session_id)] == RESUMING


async def test_a_record_that_cannot_make_a_command_is_decided_not_retried(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remote_agents.application import interrupted_resume

    record = await rig.running()
    snapshot = await rig.snapshot(record)

    def refused(*_args: object) -> object:
        raise ValueError("conversation reference must be a bounded opaque token")

    monkeypatch.setattr(interrupted_resume, "_resolved", refused)
    (report,) = await rig.run_pass().run(snapshot)

    assert report.outcome == "skipped:launch_error"
    assert rig.commands == []


async def test_the_first_outcome_stands_except_over_the_intent(rig: Rig) -> None:
    at = NOW

    assert await rig.outcomes.record("s", RESUMING, decided_at=at)
    assert await rig.outcomes.record("s", "resumed:new", decided_at=at)
    assert not await rig.outcomes.record("s", "skipped:launch_error", decided_at=at)
    assert not await rig.outcomes.record("s", RESUMING, decided_at=at)
    assert (await rig.outcomes.outcomes()) == {"s": "resumed:new"}


async def test_a_nudging_intent_yields_only_to_a_final_outcome(rig: Rig) -> None:
    at = NOW

    assert await rig.outcomes.record("s", RESUMING, decided_at=at)
    assert await rig.outcomes.record("s", "nudging:new", decided_at=at)
    assert not await rig.outcomes.record("s", RESUMING, decided_at=at)
    assert not await rig.outcomes.record("s", "nudging:other", decided_at=at)
    assert await rig.outcomes.record("s", "resumed:new; not nudged:dialog", decided_at=at)
    assert not await rig.outcomes.record("s", "resumed:new", decided_at=at)
    assert (await rig.outcomes.outcomes()) == {"s": "resumed:new; not nudged:dialog"}


# --- Task 2.4: the nudge, typed once into a session that was mid-turn ------------------------


async def _mid_turn(rig: Rig, **kwargs) -> SessionRecord:
    record = await rig.running(**kwargs)
    rig.mid_turn.add(str(record.session_id))
    return record


async def test_nudge_a_marked_session_gets_exactly_one_send(rig: Rig) -> None:
    marked = await _mid_turn(rig)
    idle = await rig.running(created=CREATED + timedelta(minutes=1))
    snapshot = await rig.snapshot(marked, idle)

    reports = await rig.run_pass().run(snapshot)

    by_record = {report.record: report for report in reports}
    new = by_record[marked].new_session_id
    assert rig.terminal.prompts == [(new, NUDGE)]
    assert by_record[marked].outcome == f"resumed:{new}"
    assert (await rig.outcomes.outcomes())[str(marked.session_id)] == f"resumed:{new}"


async def test_nudge_an_unmarked_session_gets_none(rig: Rig) -> None:
    record = await rig.running()

    await rig.run_pass().run(await rig.snapshot(record))

    assert rig.terminal.prompts == []


async def test_nudge_waits_out_a_booting_agent_every_three_seconds(rig: Rig) -> None:
    record = await _mid_turn(rig)
    rig.terminal.prompt_deliveries = [
        PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY),
        PromptDelivery(PromptOutcome.REFUSED, PromptReason.UNRECOGNISED),
    ]

    (report,) = await rig.run_pass().run(await rig.snapshot(record))

    assert len(rig.terminal.prompts) == 3
    assert rig.sleeps == [RETRY_SECONDS, RETRY_SECONDS]
    assert report.outcome == f"resumed:{report.new_session_id}"


async def test_nudge_gives_up_after_the_startup_patience(rig: Rig) -> None:
    record = await _mid_turn(rig)
    rig.terminal.prompt_deliveries = [
        PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY) for _ in range(100)
    ]

    (report,) = await rig.run_pass().run(await rig.snapshot(record))

    assert report.outcome == f"resumed:{report.new_session_id}; not nudged:not_ready"
    assert sum(rig.sleeps) >= STARTUP_PATIENCE.total_seconds()
    assert sum(rig.sleeps) < STARTUP_PATIENCE.total_seconds() + RETRY_SECONDS + 1


async def test_nudge_a_trust_dialog_gets_none_and_says_why(rig: Rig) -> None:
    """Up on its folder-trust dialog: typing would answer the dialog (DEC-099)."""
    record = await _mid_turn(rig)

    async def untrusted(command: ResumeCommand):
        outcome = await rig.service.resume(command)
        return ResumeOutcome(replace(outcome.record, state=SessionState.UNTRUSTED), created=True)

    (report,) = await rig.run_pass(resume=untrusted).run(await rig.snapshot(record))

    assert rig.terminal.prompts == []
    assert report.outcome == f"resumed:{report.new_session_id}; not nudged:dialog"


async def test_nudge_a_dialog_on_the_screen_is_given_up_at_once(rig: Rig) -> None:
    record = await _mid_turn(rig)
    rig.terminal.prompt_deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)]

    (report,) = await rig.run_pass().run(await rig.snapshot(record))

    assert len(rig.terminal.prompts) == 1 and rig.sleeps == []
    assert report.outcome.endswith("; not nudged:dialog")


@pytest.mark.parametrize("answer", ["unconfirmed", "raises"])
async def test_nudge_an_unconfirmed_send_is_not_repeated(rig: Rig, answer: str) -> None:
    record = await _mid_turn(rig)
    sends: list[str] = []

    async def send(session_id: SessionId, text: str) -> PromptDelivery:
        sends.append(text)
        if answer == "raises":
            raise RuntimeError("tmux went away mid-paste")
        return PromptDelivery(PromptOutcome.UNCONFIRMED, PromptReason.SUBMIT_NOT_SEEN)

    resume_pass = rig.run_pass()
    resume_pass._send = send  # type: ignore[attr-defined]
    (report,) = await resume_pass.run(await rig.snapshot(record))

    assert sends == [NUDGE]
    assert report.outcome.endswith("; not nudged:unconfirmed")


async def test_nudge_killed_mid_send_is_never_typed_again(rig: Rig) -> None:
    """The `nudging:` intent is on disk before the send: a later start gives it up."""
    record = await _mid_turn(rig)
    snapshot = await rig.snapshot(record)

    async def killed(session_id: SessionId, text: str) -> PromptDelivery:
        raise asyncio.CancelledError

    resume_pass = rig.run_pass()
    resume_pass._send = killed  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await resume_pass.run(snapshot)
    (stored,) = (await rig.outcomes.outcomes()).values()
    assert stored.startswith("nudging:")

    await rig.run_pass().run(snapshot)

    (stored,) = (await rig.outcomes.outcomes()).values()
    assert (
        stored == f"resumed:{stored.removeprefix('resumed:').split(';')[0]}; not nudged:unconfirmed"
    )
    assert rig.terminal.prompts == []


async def test_nudge_two_sessions_are_nudged_side_by_side(rig: Rig) -> None:
    """Both resumes come first, then both nudges, so one slow agent does not hold the other."""
    first = await _mid_turn(rig)
    second = await _mid_turn(rig, created=CREATED + timedelta(minutes=1))
    order: list[str] = []

    async def resume(command: ResumeCommand):
        order.append("resume")
        return await rig.service.resume(command)

    second_sent = asyncio.Event()

    async def send(session_id: SessionId, text: str) -> PromptDelivery:
        order.append("send")
        if len([step for step in order if step == "send"]) == 1:
            # The first nudge cannot finish until the second one has been sent: a sequential
            # loop would wait here for ever.
            await asyncio.wait_for(second_sent.wait(), timeout=2)
        else:
            second_sent.set()
        return PromptDelivery(PromptOutcome.SENT)

    resume_pass = rig.run_pass(resume=resume)
    resume_pass._send = send  # type: ignore[attr-defined]
    reports = await resume_pass.run(await rig.snapshot(first, second))

    assert order == ["resume", "resume", "send", "send"]
    assert all(";" not in report.outcome for report in reports)


async def test_nudge_stops_typing_once_the_service_drains(rig: Rig) -> None:
    """A drain while an agent boots ends the wait; nothing was typed, so it says `stopping`."""
    record = await _mid_turn(rig)
    rig.terminal.prompt_deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY)]

    async def drain_while_waiting(seconds: float) -> None:
        await rig.locks.drain()

    resume_pass = rig.run_pass()
    resume_pass._sleep = drain_while_waiting  # type: ignore[attr-defined]
    (report,) = await resume_pass.run(await rig.snapshot(record))

    assert len(rig.terminal.prompts) == 1
    assert report.outcome == f"resumed:{report.new_session_id}; not nudged:stopping"


async def test_nudge_a_failed_outcome_write_costs_no_sibling_its_report(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await _mid_turn(rig)
    second = await _mid_turn(rig, created=CREATED + timedelta(minutes=1))
    record = rig.outcomes.record

    async def locked_once(session_id: str, outcome: str, *, decided_at: datetime) -> bool:
        if session_id == str(first.session_id) and outcome.startswith("resumed:"):
            raise RuntimeError("database is locked")
        return await record(session_id, outcome, decided_at=decided_at)

    monkeypatch.setattr(rig.outcomes, "record", locked_once)
    reports = await rig.run_pass().run(await rig.snapshot(first, second))

    assert [report.record for report in reports] == [first, second]
    assert len(rig.terminal.prompts) == 2
    stored = await rig.outcomes.outcomes()
    assert stored[str(first.session_id)].startswith("nudging:")
    assert stored[str(second.session_id)] == reports[1].outcome


async def test_nudge_a_refusal_says_why_by_its_reason(rig: Rig) -> None:
    record = await _mid_turn(rig)
    rig.terminal.prompt_deliveries = [
        PromptDelivery(PromptOutcome.REFUSED, PromptReason.NO_COMPOSER)
    ]

    (report,) = await rig.run_pass().run(await rig.snapshot(record))

    assert report.outcome.endswith("; not nudged:no_composer")


# --- gate round 2: every decision on disk first; a busy daemon; a clock that steps ----------


async def test_a_pass_killed_at_one_session_leaves_every_later_one_its_intent(rig: Rig) -> None:
    """Reconcile has ended them all by now: a session with no row would never come back."""
    first = await rig.running()
    second = await rig.running(created=CREATED + timedelta(minutes=1))
    third = await rig.running(created=CREATED + timedelta(minutes=2))
    skipped = await rig.running(mark=None, created=CREATED + timedelta(minutes=3))
    snapshot = await rig.snapshot(first, second, third, skipped)
    await rig.end_as_reconcile_does(first, second, third, skipped)

    async def killed_at_the_second(command: ResumeCommand):
        if rig.commands:
            raise asyncio.CancelledError
        rig.commands.append(command)
        return await rig.service.resume(command)

    with pytest.raises(asyncio.CancelledError):
        await rig.run_pass(resume=killed_at_the_second).run(snapshot)

    outcomes = await rig.outcomes.outcomes()
    assert outcomes[str(second.session_id)] == RESUMING
    assert outcomes[str(third.session_id)] == RESUMING
    assert outcomes[str(skipped.session_id)] == "skipped:no-conversation"

    reports = await rig.run_pass().run(
        await rig.snapshot(*[await _get(rig, r) for r in (first, second, third, skipped)])
    )

    assert {str(r.record.session_id): r.outcome.split(":")[0] for r in reports} == {
        str(second.session_id): "resumed",
        str(third.session_id): "resumed",
    }


async def test_a_busy_daemon_defers_the_rest_of_its_profile_without_waiting_again(
    rig: Rig,
) -> None:
    first = await rig.running()
    second = await rig.running(created=CREATED + timedelta(minutes=1))
    calls: list[ResumeCommand] = []

    async def busy(command: ResumeCommand):
        calls.append(command)
        raise CodexRemoteControlBusy("busy")

    reports = await rig.run_pass(resume=busy).run(await rig.snapshot(first, second))

    assert len(calls) == 1
    assert [report.outcome for report in reports] == [DEFERRED, DEFERRED]
    assert set((await rig.outcomes.outcomes()).values()) == {RESUMING}


async def test_nudge_patience_is_counted_so_a_clock_stepping_back_still_gives_up(
    rig: Rig,
) -> None:
    record = await _mid_turn(rig)
    rig.terminal.prompt_deliveries = [
        PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY) for _ in range(100)
    ]

    async def stepping_back(seconds: float) -> None:
        rig.sleeps.append(seconds)
        rig.clock -= timedelta(hours=1)

    resume_pass = rig.run_pass()
    resume_pass._sleep = stepping_back  # type: ignore[attr-defined]
    (report,) = await resume_pass.run(await rig.snapshot(record))

    assert report.outcome.endswith("; not nudged:not_ready")
    assert len(rig.sleeps) == STARTUP_RETRIES
