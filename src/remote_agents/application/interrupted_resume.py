"""Resume, once, the sessions a restart interrupted (DEC-127).

A host reboot or a tmux crash takes every managed pane with it, and the first reconcile then
ends each RUNNING record as `reconciled_terminal_missing`. This module decides, from a snapshot
taken *before* that reconcile, which of those records come back in their own conversation and
which are skipped, and why. The decision is pure: the pass that acts on it, and the tests, read
the same table.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, SessionId, SessionRecord, SessionState
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.rollover_store import Rollover

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
    """False when the start time could not be read: nothing is resumed on a guess."""
    turn_marker_sessions: frozenset[str]
    """Sessions whose turn marker survived: they were mid-turn when they died (DEC-104)."""
    conversation_marks: Mapping[str, str]
    """Each session's last conversation mark, by session id."""
    rollovers: tuple[Rollover, ...]
    resumable_profiles: frozenset[ProfileId]
    """Profiles whose agent can reopen a chosen conversation (`resume_capable`)."""


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
            if _interrupted(session, snapshot.server_started_at)
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


def _interrupted(session: SessionAtRestart, server_started_at: datetime | None) -> bool:
    if session.record.state is not SessionState.RUNNING or session.pane_listed:
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


def _held_conversations(record: SessionRecord, marks: Mapping[str, str]) -> tuple[str, ...]:
    """Every conversation a live record may be in: its mark and the one it was resumed from."""
    return tuple(
        value
        for value in (marks.get(str(record.session_id)), record.resume_source_id)
        if value is not None
    )
