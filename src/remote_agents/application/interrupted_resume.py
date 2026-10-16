"""Resume, once, the sessions a restart interrupted (DEC-127).

A host reboot or a tmux crash takes every managed pane with it, and the first reconcile then
ends each RUNNING record as `reconciled_terminal_missing`. This module decides, from a snapshot
taken *before* that reconcile, which of those records come back in their own conversation and
which are skipped, and why. The decision is pure: the pass that acts on it, and the tests, read
the same table.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from remote_agents.application.commands import UNATTENDED_LAUNCH_WAIT_SECONDS, ResumeCommand
from remote_agents.application.errors import DuplicateCommandError, MutationsDraining
from remote_agents.application.limit_resume import NUDGE
from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.application.schedules import RETRY_SECONDS, STARTUP_RETRIES
from remote_agents.application.services import ResumeOutcome
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, SessionId, SessionRecord, SessionState
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy
from remote_agents.ports.interrupted_resumes import (
    NUDGING,
    RESUMED,
    RESUMING,
    SKIPPED,
    InterruptedResumes,
)
from remote_agents.ports.rollover_store import Rollover
from remote_agents.ports.terminal import PromptDelivery, PromptReason
from remote_agents.ports.worktree_store import Worktree

_LOG = logging.getLogger(__name__)

RESUMED_LABEL = "resumed after restart"
"""The name a resumed session carries, so the owner can tell it from one they started."""

NOT_NUDGED = "not nudged"
"""Appended to `resumed:<id>` as `; not nudged:<reason>` when the nudge was not typed."""

#: What a booting agent shows on its way to an idle composer, so the nudge waits it out, as a
#: schedule's first prompt does.
_BOOTING = frozenset(
    {DeliveryVerdict.WAIT, DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED}
)

DEFERRED = "deferred"
"""A report's outcome when the resume was refused before its key was claimed: the intent
stays `resuming`, and the next start tries again."""

SERVER_CLOCK_SLACK = timedelta(seconds=1)
"""tmux reports its start time in whole seconds, and the first launch on a host starts the
server itself a moment after its record is written. A server counts as newer than a record only
when it started more than this after the record was created."""

HANDED_OFF: frozenset[RolloverState] = frozenset(
    {
        RolloverState.SUCCESSOR_ACCEPTED,
        RolloverState.PREDECESSOR_STOPPING,
        RolloverState.COMPLETED,
        RolloverState.STOP_FAILED,
    }
)
"""Rollover states in which the successor has accepted the work: the predecessor no longer
holds it, so bringing the predecessor back would run the work twice (DEC-115)."""


class ResumeSkip(StrEnum):
    """Why an interrupted session is not resumed; persisted as `skipped:<value>`."""

    NOT_RESUMABLE = "not-resumable"
    HANDED_OFF = "handed-off"
    ALREADY_OPEN = "already-open"
    NO_CONVERSATION = "no-conversation"
    SUPERSEDED = "superseded"
    """A session an earlier start left `resuming` whose conversation a newer session has opened
    since -- the owner resumed it by hand, or the earlier launch got that far. Never relaunched:
    the work moved on without it."""


@dataclass(frozen=True, slots=True)
class SessionAtRestart:
    """One record as it stood before the first reconcile, and whether its pane is listed."""

    record: SessionRecord
    pane_listed: bool


@dataclass(frozen=True, slots=True)
class RestartSnapshot:
    """What `serve` observed at startup, before reconcile could end anything."""

    sessions: tuple[SessionAtRestart, ...]
    server_started_at: datetime | None
    """None when no tmux server is running."""
    server_known: bool
    """False when anything the decision needs could not be read -- the server's start time
    above all: nothing is resumed on a guess."""
    turn_marker_sessions: frozenset[str]
    """Sessions whose turn marker survived: they were mid-turn when they died (DEC-104)."""
    conversation_marks: Mapping[str, str]
    """Each session's last conversation mark, by session id."""
    rollovers: tuple[Rollover, ...]
    resumable_profiles: frozenset[ProfileId]
    """Profiles whose agent can reopen a chosen conversation (`resume_capable`)."""
    pending: frozenset[str] = frozenset()
    """Sessions an earlier start left at `resuming`. They were judged interrupted then, and
    reconcile has ended them since, so they count whatever their state while no pane is
    listed for them."""


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """One interrupted session: resume it in `conversation_id`, or skip it for `skip`."""

    record: SessionRecord
    conversation_id: ProviderConversationId | None
    skip: ResumeSkip | None
    nudge: bool
    """Type the resume nudge once it is back: only a session that was mid-turn (owner ruling)."""


def plan_resumes(snapshot: RestartSnapshot) -> tuple[ResumeDecision, ...]:
    """Decide every interrupted session once, oldest first.

    **Interrupted means lost with its server.** A RUNNING record counts only when its pane is
    not listed and the server is absent or started after the record was created. A
    service-only restart leaves panes alive, and one pane killed under a live server is the
    owner's doing, not this feature's case. Only RUNNING records count (DEC-020). There is no
    age limit (owner ruling).
    """
    if not snapshot.server_known:
        return ()
    interrupted = sorted(
        (
            session.record
            for session in snapshot.sessions
            if _interrupted(session, snapshot.server_started_at, snapshot.pending)
        ),
        key=lambda record: (record.created_at, str(record.session_id)),
    )
    handed_off = {
        rollover.predecessor_session_id
        for rollover in snapshot.rollovers
        if rollover.state in HANDED_OFF
    }
    held = {
        (session.record.profile_id, conversation)
        for session in snapshot.sessions
        if session.pane_listed and session.record.state is not SessionState.ENDED
        for conversation in _held_conversations(session.record, snapshot.conversation_marks)
    }
    decisions: list[ResumeDecision] = []
    for record in interrupted:
        conversation = _conversation(record, snapshot.conversation_marks)
        skip = _skip(record, conversation, snapshot.resumable_profiles, handed_off, held)
        if (
            skip is None
            and conversation is not None
            and str(record.session_id) in snapshot.pending
            and _opened_since(record, conversation, snapshot)
        ):
            skip = ResumeSkip.SUPERSEDED
        if skip is None and conversation is not None:
            held.add((record.profile_id, conversation.value))
        decisions.append(
            ResumeDecision(
                record,
                conversation,
                skip,
                nudge=skip is None and str(record.session_id) in snapshot.turn_marker_sessions,
            )
        )
    return tuple(decisions)


def _interrupted(
    session: SessionAtRestart, server_started_at: datetime | None, pending: frozenset[str]
) -> bool:
    if session.pane_listed:
        return False
    if str(session.record.session_id) in pending:
        return True
    if session.record.state is not SessionState.RUNNING:
        return False
    return (
        server_started_at is None
        or server_started_at > session.record.created_at + SERVER_CLOCK_SLACK
    )


def _skip(
    record: SessionRecord,
    conversation: ProviderConversationId | None,
    resumable: frozenset[ProfileId],
    handed_off: set[SessionId],
    held: set[tuple[ProfileId, str]],
) -> ResumeSkip | None:
    if record.profile_id not in resumable:
        return ResumeSkip.NOT_RESUMABLE
    if record.session_id in handed_off:
        return ResumeSkip.HANDED_OFF
    if conversation is None:
        return ResumeSkip.NO_CONVERSATION
    if (record.profile_id, conversation.value) in held:
        return ResumeSkip.ALREADY_OPEN
    return None


def _conversation(record: SessionRecord, marks: Mapping[str, str]) -> ProviderConversationId | None:
    """The session's conversation mark, else its resume source; identifiers only (DEC-067)."""
    for candidate in (marks.get(str(record.session_id)), record.resume_source_id):
        if candidate is None:
            continue
        try:
            return ProviderConversationId(candidate)
        except ValueError:
            continue
    return None


def _opened_since(
    record: SessionRecord, conversation: ProviderConversationId, snapshot: RestartSnapshot
) -> bool:
    """Whether a session newer than `record` has been in the same conversation, in any state."""
    return any(
        other.record.session_id != record.session_id
        and other.record.profile_id == record.profile_id
        and other.record.created_at > record.created_at
        and conversation.value in _held_conversations(other.record, snapshot.conversation_marks)
        for other in snapshot.sessions
    )


def _held_conversations(record: SessionRecord, marks: Mapping[str, str]) -> tuple[str, ...]:
    """Every conversation a live record may be in: its mark and the one it was resumed from."""
    return tuple(
        value
        for value in (marks.get(str(record.session_id)), record.resume_source_id)
        if value is not None
    )


@dataclass(frozen=True, slots=True)
class ResumeReport:
    """What the pass did for one interrupted session, for the summary notice."""

    record: SessionRecord
    outcome: str
    """`resumed:<id>`, `skipped:<reason>`, or `DEFERRED`."""
    new_session_id: SessionId | None = None
    nudge: bool = False
    """The decision asked for the resume nudge (Task 2.4 types it)."""


class InterruptedResumePass:
    """Act on `plan_resumes` once per interrupted session, durably (DEC-114, DEC-127).

    Every decision is written before anything is launched: a skip as its final outcome, a
    resume as the `RESUMING` intent first. A session whose row holds a final outcome is never
    acted on again and is not reported. One holding `RESUMING` is tried again; its
    `restart:<session>` key is claimed at most once, so the retry launches only when the
    earlier attempt never reached the claim.

    **Every resume comes first, then the nudges, side by side.** A session that was mid-turn
    gets `limit_resume.NUDGE` typed once (DEC-099, DEC-109), tried every `RETRY_SECONDS`, up to
    `STARTUP_RETRIES` more times, while its agent boots, and never again after a send that may
    have typed.
    A `nudging:<id>` intent is written before the first send, so a start that finds one left
    over gives it up as unconfirmed rather than typing it twice.

    **An error is final only once the key is claimed.** Before the claim nothing can have
    started, so the intent is left for the next start (`DEFERRED`); after it the launch may
    have got part-way, so it is decided as `skipped:launch_error` and never tried again. When
    the claim cannot be read, the intent is left too: the key still refuses a second launch.
    """

    def __init__(
        self,
        outcomes: InterruptedResumes,
        *,
        resume: Callable[[ResumeCommand], Awaitable[ResumeOutcome]],
        folder_of: Callable[[SessionId], Awaitable[Worktree | None]],
        key_claimed: Callable[[str], Awaitable[bool]],
        send: Callable[[SessionId, str], Awaitable[PromptDelivery]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stopping: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime],
    ) -> None:
        self._outcomes = outcomes
        self._resume = resume
        self._folder_of = folder_of
        self._key_claimed = key_claimed
        self._send = send
        self._sleep = sleep
        self._stopping = stopping
        self._now = now

    async def run(self, snapshot: RestartSnapshot) -> tuple[ResumeReport, ...]:
        """Resume or skip every interrupted session the snapshot shows, and say what each came
        to. When the service starts draining, the pass stops there and answers what it has done
        so far; every session it did not reach keeps its intent for the next start.

        **Every decision is written before the first launch.** The first reconcile has already
        ended these records, so a session whose row is missing when the process dies is never
        seen as interrupted again: a skip is written as its outcome and a resume as its intent,
        all of them, before anything is launched."""
        decided = await self._outcomes.outcomes()
        await self._give_up_interrupted_nudges(decided)
        reports: dict[str, ResumeReport] = {}
        order: list[str] = []
        to_resume: list[ResumeDecision] = []
        for decision in plan_resumes(snapshot):
            session = str(decision.record.session_id)
            current = decided.get(session)
            if current is not None and current != RESUMING:
                continue
            order.append(session)
            if decision.skip is not None:
                reports[session] = await self._decide(decision, f"{SKIPPED}:{decision.skip.value}")
                continue
            if current is None:
                await self._outcomes.record(session, RESUMING, decided_at=self._now())
            to_resume.append(decision)
        busy: set[ProfileId] = set()
        draining = False
        for decision in to_resume:
            session = str(decision.record.session_id)
            if self._stopping():
                draining = True
                break
            if decision.record.profile_id in busy:
                # Its shared daemon refused one launch already: each more would wait it out too.
                reports[session] = ResumeReport(decision.record, DEFERRED)
                continue
            try:
                reports[session] = await self._resumed(decision)
            except MutationsDraining:
                draining = True
                break
            except CodexRemoteControlBusy:
                busy.add(decision.record.profile_id)
                _LOG.warning("resuming %s waited out the daemon guard", decision.record.session_id)
                reports[session] = ResumeReport(decision.record, DEFERRED)
        if draining:
            _LOG.warning("the service began stopping; the restart resume pass stops here")
        nudged = await asyncio.gather(
            *(
                self._nudged(reports[session], stopping=draining)
                for session in order
                if session in reports
            )
        )
        return tuple(nudged)

    async def _give_up_interrupted_nudges(self, decided: Mapping[str, str]) -> None:
        """Finish every `nudging:` intent an earlier start left: it may have typed (DEC-099)."""
        for session, outcome in decided.items():
            if outcome.startswith(f"{NUDGING}:"):
                new = outcome.removeprefix(f"{NUDGING}:")
                final = f"{RESUMED}:{new}; {NOT_NUDGED}:unconfirmed"
                await self._outcomes.record(session, final, decided_at=self._now())
                _LOG.warning("the nudge into %s was interrupted; not typed again", new)

    async def _nudged(self, report: ResumeReport, *, stopping: bool) -> ResumeReport:
        """Type the nudge into a report's resumed session if it asks for one, and finish it."""
        if not report.outcome.startswith(f"{NUDGING}:"):
            return report
        assert report.new_session_id is not None  # only a resumed report holds the intent
        reason = "stopping" if stopping else await self._nudge(report.new_session_id)
        resumed = f"{RESUMED}:{report.new_session_id}"
        final = resumed if reason is None else f"{resumed}; {NOT_NUDGED}:{reason}"
        try:
            await self._outcomes.record(
                str(report.record.session_id), final, decided_at=self._now()
            )
        except Exception:
            # Never raised into the gather: one failed write would drop every sibling's report
            # while their nudges carried on. The intent stays, and the next start gives it up as
            # unconfirmed -- the safe reading, never a second send.
            _LOG.exception("the nudge outcome for %s could not be written", report.new_session_id)
        return ResumeReport(report.record, final, report.new_session_id, report.nudge)

    async def _nudge(self, session_id: SessionId) -> str | None:
        """None once the nudge landed, else why it was not typed. Never raises.

        Patience is counted in tries, never read off the wall clock: this runs just after a
        boot, when the clock is most likely to step, and a step back would wait for ever."""
        retries = 0
        while True:
            if self._stopping():
                # Nothing has been typed: every earlier try answered a verdict that types nothing.
                return "stopping"
            try:
                delivery = await self._send(session_id, NUDGE)
            except Exception:
                # The send never raises by contract; a raise here may have typed (DEC-099).
                _LOG.exception("typing the resume nudge into %s failed partway", session_id)
                return DeliveryVerdict.UNCONFIRMED.value
            verdict = delivery_verdict(delivery)
            if verdict is DeliveryVerdict.SENT:
                return None
            if verdict not in _BOOTING:
                if verdict is DeliveryVerdict.REFUSED and delivery.reason is not None:
                    return delivery.reason.value
                if delivery.reason is PromptReason.MENU:
                    return PromptReason.MENU.value
                return verdict.value
            if retries >= STARTUP_RETRIES:
                if verdict is DeliveryVerdict.WAIT:
                    return "not_ready"
                return verdict.value
            retries += 1
            await self._sleep(RETRY_SECONDS)

    async def _resumed(self, decision: ResumeDecision) -> ResumeReport:
        record = decision.record
        assert decision.conversation_id is not None  # a decision that is not a skip has one
        try:
            worktree = await self._folder_of(record.session_id)
        except Exception:
            _LOG.exception(
                "the folder of interrupted session %s could not be read", record.session_id
            )
            return ResumeReport(record, DEFERRED)
        if worktree is not None and worktree.removed_at is not None:
            return await self._decide(decision, f"{SKIPPED}:worktree_removed")
        try:
            command = ResumeCommand(
                record.project_id,
                record.profile_id,
                _resolved(
                    record,
                    decision.conversation_id,
                    None if worktree is None else worktree.path,
                    self._now(),
                ),
                f"restart:{record.session_id}",
                guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS,
                label=RESUMED_LABEL,
            )
        except ValueError:
            # Nothing was claimed, but the same record would fail the same way on every start.
            _LOG.exception("interrupted session %s has no resumable command", record.session_id)
            return await self._decide(decision, f"{SKIPPED}:launch_error")
        try:
            outcome = await self._resume(command)
        except DuplicateCommandError:
            # The key was claimed by an earlier start that never recorded what it did.
            return await self._decide(decision, f"{SKIPPED}:interrupted")
        except MutationsDraining:
            raise
        except CodexRemoteControlBusy:
            # Refused at the Codex daemon guard, before the key was claimed: nothing started.
            # Raised on, so `run` defers this profile's other sessions without waiting again.
            raise
        except Exception:
            _LOG.exception("resuming interrupted session %s failed", record.session_id)
            if await self._claimed(command.idempotency_key):
                # It may have got part-way: decided, never tried again.
                return await self._decide(decision, f"{SKIPPED}:launch_error")
            return ResumeReport(record, DEFERRED)
        if outcome.worktree_removed:
            return await self._decide(decision, f"{SKIPPED}:worktree_removed")
        if not outcome.created:
            return await self._decide(decision, f"{SKIPPED}:already-open")
        if outcome.record.state is SessionState.FAILED:
            return await self._decide(decision, f"{SKIPPED}:launch_failed")
        new = outcome.record.session_id
        if not decision.nudge:
            outcome_text = f"{RESUMED}:{new}"
        elif outcome.record.state is SessionState.UNTRUSTED:
            # Up on its folder-trust dialog: typing would answer the dialog (DEC-099).
            outcome_text = f"{RESUMED}:{new}; {NOT_NUDGED}:{DeliveryVerdict.DIALOG.value}"
        else:
            outcome_text = f"{NUDGING}:{new}"
        report = await self._decide(decision, outcome_text)
        return ResumeReport(record, report.outcome, new, decision.nudge)

    async def _claimed(self, key: str) -> bool:
        """Whether the resume's key is claimed; False when that cannot be read, which leaves
        the intent for the next start, where the key refuses a second launch anyway."""
        try:
            return await self._key_claimed(key)
        except Exception:
            _LOG.exception("could not read whether %s was claimed", key)
            return False

    async def _decide(self, decision: ResumeDecision, outcome: str) -> ResumeReport:
        session = str(decision.record.session_id)
        if not await self._outcomes.record(session, outcome, decided_at=self._now()):
            # Another writer decided it first; the first outcome stands, so report that one.
            stored = (await self._outcomes.outcomes()).get(session, outcome)
            _LOG.warning("interrupted session %s was already decided as %s", session, stored)
            return ResumeReport(decision.record, stored)
        return ResumeReport(decision.record, outcome)


def _resolved(
    record: SessionRecord,
    conversation: ProviderConversationId,
    folder: Path | None,
    now: datetime,
) -> ResolvedConversation:
    """The conversation as a resume takes it. The reference is opaque and never shown: it is
    derived from the id the way the provider catalogues derive theirs."""
    digest = hashlib.sha256(f"{record.profile_id}:{conversation.value}".encode()).hexdigest()
    return ResolvedConversation(
        ConversationSummary(
            ConversationReference(f"c-{digest[:32]}"),
            record.profile_id,
            record.project_id,
            ConversationState.RESUMABLE,
            now,
        ),
        conversation,
        folder,
    )
