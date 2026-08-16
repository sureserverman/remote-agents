"""Read what the agent hooks spooled, and say what it means, once.

The spool is the boundary between a hook running inside somebody else's process and this
service. Everything on the far side is untrusted in the ordinary sense -- it may be absent,
truncated, or shaped differently by a newer agent release -- so every step here drops what it
cannot read rather than raising, and a record that reaches no mapping is dropped rather than
guessed at. Reporting the wrong reason an agent stopped is worse than reporting nothing: the
owner acts on these.

Two events are dropped for a second reason, which is not that they cannot be read but that the
owner has nothing to do about either. Both were mapped once, and they fail that bar
differently. `SessionEnd` reports an exit the owner usually caused -- pressing Stop types
`/exit` into the pane -- and it cannot say so: every `reason` it carries, `logout` and `clear`
and the rest, maps to one sentence, so at best it repeats an action they just took and at worst
it announces an ending it cannot characterise. The sixty-second idle notification fails it for
the opposite reason: it is unreliable in both directions, and on the occasions it is right,
`permission_prompt` or `agent_needs_input` has usually already said the same thing as a fact
rather than a guess. Everything built from these records arrives unprompted, so an observation
that is true and carries nothing to do is a cost with no return -- and the mapping is the right
place to decide that, because a kind that is never produced cannot then be rendered,
rate-limited, or delivered by mistake somewhere further down.

Draining deletes. A record turned into activity is gone from disk before it is returned, so a
service that restarts halfway through a delivery cannot tell the owner the same thing twice.
The cost is the opposite failure -- a crash between the delete and the send loses one
notification -- and that is the right way round for a message that says an agent is waiting.

DEC-026 re-examined that cost at the magnitude an outage reaches, where a Telegram that is
refusing sends leaves a hundred undelivered notifications in the notifier's memory rather than
one, and left it as recorded: no durable queue and no schema change, because the session is the
authoritative record of what an agent did either way, so what a restart takes is the owner being
told rather than anything only the notification knew. It is a reaffirmation of DEC-013 at a size
DEC-013 did not consider, not a change to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from remote_agents.domain.models import SessionId, SessionState
from remote_agents.ports.agent_activity import (
    HOOK_SOURCED_PROFILES,
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    bounded_detail_line,
)
from remote_agents.ports.session_identity import safe_session_id
from remote_agents.ports.session_store import SessionStore

_LOG = logging.getLogger(__name__)

#: The `%Y%m%dT%H%M%S%fZ` stamp `_write_privately` puts in every name it creates.
_STAMP = re.compile(r"\d{8}T\d{12}Z")

#: How many records one pass may take. The drain runs on the same event loop that long-polls
#: Telegram and captures panes, so "however many are there" is not a bound: a service that was
#: down for a day, or a hook that fired on every tool call, would stall every other thing the
#: loop owes the owner. What is left over is not lost -- the next pass takes it, seconds later.
MAXIMUM_DRAIN = 200

#: How long a `.pending-*.tmp` must sit untouched before it counts as abandoned rather
#: than in progress. Far longer than any write that is still going to finish.
_ABANDONED_TEMPORARY_SECONDS = 3600.0

#: The largest file this will read back as one record. The hook caps its *input* at 32 KiB
#: and writes a record far smaller than that, so this is generous by orders of magnitude; it
#: exists because the writer on the far side of the spool is not guaranteed to be that hook.
MAXIMUM_RECORD_BYTES = 65_536

#: How long one pane capture may take before it counts as a failed read. Generous for a
#: local tmux, and finite so a wedged server costs one pass rather than the whole watch.
_CAPTURE_TIMEOUT_SECONDS = 15.0

# Only the reasons that answer "why did it stop". Every other value these fields can take --
# authentication_failed, auth_success, elicitation_*, and whatever upstream adds next -- is
# absent on purpose, and an event carrying one is dropped by the lookup below.
_STOP_FAILURES = {
    "rate_limit": ActivityKind.LIMIT_REACHED,
    # Not the same news, and not the same next move: a rate limit is waited out, an output
    # ceiling is continued from. One sentence for both told the owner the alarming one.
    "max_output_tokens": ActivityKind.OUTPUT_LIMIT,
}
# Both of these are the agent saying it is stuck, which is why both are REPORTED. Upstream's
# third one -- a sixty-second idle timer -- is absent for a different reason than the values
# above: it answers "why did it stop" and answers it wrongly often enough to have a recorded
# history of doing so, on an agent that had merely gone quiet while thinking. It was carried
# for a while as an inferred `needs_answer` on the argument that a hedged sentence makes a
# weak signal safe to send, which it does not: a hedge changes how a message reads, never
# whether it was worth sending. When the timer is right, one of these two says the same thing.
_NOTIFICATIONS = {
    "permission_prompt": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED),
    "agent_needs_input": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED),
}


def drain_activity(activity_directory: Path) -> tuple[AgentActivity, ...]:
    """Take up to `MAXIMUM_DRAIN` of the oldest spooled records and return what they mean.

    The rest stay on disk for the next pass, seconds later; this does not leave the spool
    empty when it is backlogged, and a caller that assumed it did would stop polling with
    records still waiting.

    *Oldest* is the load-bearing word. The stamp is in the filename so the drain can order
    what it finds, but the session id is in front of it, so sorting names sorts by session --
    and the bound would then truncate whole sessions rather than the newest records, letting
    a session whose id sorts late lose every pass while a busier one keeps winning. Ordering
    after the truncation cannot repair that: by then the records that should have been taken
    are the ones left behind.
    """
    _clear_abandoned_temporaries(activity_directory)
    try:
        paths = sorted(activity_directory.glob("*.json"), key=_written_at)[:MAXIMUM_DRAIN]
    except OSError:
        # A spool that does not exist yet is the ordinary case before the first managed
        # launch, and one that cannot be listed is a problem for the operator, not for the
        # poll that found it. Either way there is nothing to deliver this pass.
        return ()
    drained = list(_drained(paths))
    try:
        return tuple(sorted(drained, key=lambda activity: activity.observed_at))
    except TypeError:
        # Sorting is the one step here that can still fail on data that individually parsed:
        # comparing a naive timestamp against an aware one raises. `_moment` rejects naive
        # values so this should be unreachable, but the records are already deleted by now,
        # and an ordering problem is not worth every pending activity in the batch. Unsorted
        # is a worse answer than sorted; it is a far better one than none.
        _LOG.warning("delivering %d activity records unsorted", len(drained))
        return tuple(drained)


def _clear_abandoned_temporaries(activity_directory: Path) -> None:
    """Collect the temporaries a killed hook left behind, and never one still being written.

    The spool writes each record to a `.pending-*.tmp` and links it into place, so a record is
    visible under its final name only once every byte is there. A hook killed between those
    two steps leaves the temporary, and nothing collected it: the drain globs `*.json`, and
    these are deliberately named to be invisible to that glob. One per killed hook, kept for
    the life of the machine, in the owner's own spool.

    Age is the only thing separating an abandoned temporary from one being written this
    instant -- there is nothing else to ask, and deleting the second would destroy exactly the
    record the link-into-place dance exists to protect. An hour is far longer than any write
    that is still going to finish.
    """
    horizon = time.time() - _ABANDONED_TEMPORARY_SECONDS
    try:
        pending = list(activity_directory.glob(".pending-*.tmp"))
    except OSError:
        return
    for path in pending:
        with suppress(OSError):
            if path.stat().st_mtime < horizon:
                path.unlink(missing_ok=True)


def _written_at(path: Path) -> tuple[str, str]:
    """Order by the stamp the hook put in the name, falling back to the name itself.

    A name with no stamp is not one this spool wrote, and it sorts first so that foreign
    files clear out rather than accumulating in front of a bound they would otherwise sit
    behind forever. The name is the tiebreak, so the order is total and a pass is repeatable.

    The match is the leftmost one anywhere in the name, which assumes stamps appear only where
    the hook puts them. A session id is `[A-Za-z0-9_-]{1,128}` and so *could* be shaped like a
    stamp and sort ahead of its own timestamp. Nothing is granted by that: writing into this
    directory already means running as the owner, and an owner who wants to disturb the order
    can simply write an old stamp. Ordering here is a fairness property, not a security one.
    """
    found = _STAMP.search(path.name)
    return (found.group(0) if found else "", path.name)


def _drained(paths: list[Path]) -> Iterator[AgentActivity]:
    for path in paths:
        activity = _read_one(path)
        if activity is not None:
            yield activity


def _read_one(path: Path) -> AgentActivity | None:
    """Turn one file into activity, or into nothing, but never into an exception.

    Every step is guarded, deliberately. An earlier version wrapped only the read and the
    parse, which left the unlink and the mapping outside -- and both run once the pass has
    already deleted files, so anything raising there destroyed the batch exactly as a parse
    failure had. Narrowing a guard to the line that happened to fail is what turns one bug
    into two.

    The order is the invariant: *nothing is delivered that was not first removed*. Deleting
    before the record is used means a crash between here and the send loses one notification,
    which is the right way round for a message saying an agent is waiting -- and a record that
    could not be deleted is dropped rather than returned, because delivering it would repeat
    "your agent is waiting" on every pass for as long as the spool stays unwritable.
    """
    try:
        with path.open("rb") as handle:
            # One byte past the limit, so "too large" is decided without the file ever being
            # in memory. `read_bytes()` and a length check afterwards rejected the same
            # records, but only after allocating all of them: a 600 MB spool file took peak
            # memory from 16 MB to 631 MB before being judged. The `MemoryError` was caught
            # and the file unlinked, so it healed in one pass -- while there was headroom for
            # it. A file large enough to get the process killed mid-read survives the kill,
            # and `Restart=on-failure` brings the service back to read it again. Same shape
            # as `activity_spool`'s own read, on the other side of the same spool.
            raw = handle.read(MAXIMUM_RECORD_BYTES + 1)
    except Exception:
        _LOG.warning("discarding an unreadable activity record")
        raw = None

    try:
        path.unlink(missing_ok=True)
    except OSError:
        _LOG.warning("leaving an activity record that could not be deleted")
        return None

    if raw is None:
        return None
    try:
        if len(raw) > MAXIMUM_RECORD_BYTES:
            # Bounded here as well as at the hook, and not redundantly: a foreign writer is
            # tolerated by design, so the hook's cap on what it will read says nothing about
            # the size of a file found on this side. An unbounded detail reaches
            # `bounded_detail_line`'s `split()` as millions of objects before anything gets
            # to bound it.
            _LOG.warning("discarding an activity record larger than this service writes")
            return None
        record = json.loads(raw)
    except Exception:
        # Broad on purpose, and for the reason the hook's own boundary is. `json.loads`
        # answers deeply nested input with `RecursionError` and a huge one with `MemoryError`,
        # neither an `OSError` nor a `ValueError`, so both escaped a narrower guard and took
        # every already-unlinked record in the pass with them.
        _LOG.warning("discarding an unparseable activity record")
        return None
    try:
        return _activity(record) if isinstance(record, dict) else None
    except Exception:
        _LOG.warning("discarding an activity record that could not be interpreted")
        return None


def _activity(record: dict) -> AgentActivity | None:
    """Map one spooled record onto the vocabulary, or onto nothing."""
    session_id = safe_session_id(record.get("session_id"))
    observed_at = _moment(record.get("observed_at"))
    if session_id is None or observed_at is None:
        return None
    resolved = _kind(record.get("event"), record.get("reason"))
    if resolved is None:
        return None
    kind, confidence = resolved
    return AgentActivity(
        session_id=session_id,
        kind=kind,
        detail=bounded_detail_line(record.get("detail")),
        observed_at=observed_at,
        confidence=confidence,
    )


def _kind(event: object, reason: object) -> tuple[ActivityKind, ActivityConfidence] | None:
    if event == "Stop":
        return ActivityKind.COMPLETED, ActivityConfidence.REPORTED
    if event == "StopFailure":
        kind = _STOP_FAILURES.get(reason) if isinstance(reason, str) else None
        return None if kind is None else (kind, ActivityConfidence.REPORTED)
    if event == "Notification" and isinstance(reason, str):
        return _NOTIFICATIONS.get(reason)
    return None


def _moment(value: object) -> datetime | None:
    """Accept an instant, which means one that knows its own offset.

    `fromisoformat` is happy to return a naive datetime, and a naive one cannot be compared
    with an aware one -- so a single record without an offset used to raise while the batch
    was being ordered, after every file in it had already been deleted. The spool this reads
    always writes an offset; a foreign writer, which this design explicitly tolerates, need
    not. Refusing the record costs that record alone.
    """
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment


@dataclass(frozen=True, slots=True)
class QuietWatch:
    """What one session's pane looked like last time, in the only form worth keeping.

    A digest, never the capture. This is held in memory for every managed session between
    polls, and pane text is the one thing this project refuses to keep anywhere -- the store
    rejects it, and `_append_event` rejects error codes that merely mention it. A digest
    answers the only question the classifier asks ("is this the same as before?") and answers
    nothing else, which is exactly the right amount to remember.
    """

    digest: str
    unchanged_polls: int
    seen_a_change: bool
    already_reported: bool


def observe_quiet(
    watch: QuietWatch | None,
    *,
    session_id: str,
    capture: str,
    now: datetime,
    quiet_polls: int,
) -> tuple[QuietWatch, AgentActivity | None]:
    """Fold one poll into a session's watch, and say whether it just went quiet.

    Pure: the clock and the capture are given, never read, so a test drives the whole state
    machine without a terminal or a sleep, and the caller keeps the only I/O.

    Two rules carry the honesty of this signal, and both are about *not* reporting.

    A change must have been seen before an absence of change can mean anything. The claim is
    that an agent stopped producing output, which is a claim about a transition; a service
    that has only ever seen one state has not observed one, and the pane may have been
    finished for a week. Without this, restarting the service tells the owner that every idle
    pane on the host just went quiet.

    And a pane that is still quiet is not news. The report fires once per quiet spell and
    re-arms only when the pane changes again, so an agent that goes quiet, is answered, and
    goes quiet again is reported twice, while one left alone overnight is reported once.
    """
    digest = sha256(capture.encode("utf-8", errors="replace")).hexdigest()
    if watch is None:
        # The first poll establishes a baseline and claims nothing about it.
        return QuietWatch(digest, 0, seen_a_change=False, already_reported=False), None
    if digest != watch.digest:
        return QuietWatch(digest, 0, seen_a_change=True, already_reported=False), None

    unchanged = watch.unchanged_polls + 1
    due = watch.seen_a_change and not watch.already_reported and unchanged >= quiet_polls
    settled = QuietWatch(
        digest,
        unchanged,
        seen_a_change=watch.seen_a_change,
        already_reported=watch.already_reported or due,
    )
    if not due:
        return settled, None
    return settled, AgentActivity(
        session_id=session_id,
        kind=ActivityKind.QUIET,
        detail=None,
        observed_at=now,
        # Never REPORTED: nothing said this. It is inferred from a pane that stopped changing,
        # and the wording the owner sees has to be able to say so.
        confidence=ActivityConfidence.INFERRED,
    )


class PaneQuietWatcher:
    """Watch the panes of the agents that cannot report for themselves.

    Application-layer under DEC-001: the terminal is reached through a callable the caller
    supplies, so nothing here knows what tmux is, and the driver adapter never reaches a pane.

    It holds one `QuietWatch` per session between passes, which is the only state the service
    keeps about an agent's behaviour -- digests, never captures.
    """

    def __init__(
        self,
        store: SessionStore,
        capture: Callable[[SessionId], Awaitable[str]],
        *,
        quiet_polls: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._capture = capture
        self._quiet_polls = quiet_polls
        self._capture_timeout = _CAPTURE_TIMEOUT_SECONDS
        self._now = now
        self._watches: dict[str, QuietWatch] = {}

    async def poll(self) -> tuple[AgentActivity, ...]:
        """Take one look at every running session that has no hook to speak for it."""
        records = await self._store.list((SessionState.RUNNING,))
        watched = [
            record for record in records if str(record.profile_id) not in HOOK_SOURCED_PROFILES
        ]
        # Sessions that have gone away keep no state. Without this the map grows for the life
        # of the service, one entry per session ever launched.
        live = {str(record.session_id) for record in watched}
        self._watches = {key: value for key, value in self._watches.items() if key in live}

        activities = []
        for record in watched:
            key = str(record.session_id)
            try:
                # Bounded, because the failure it prevents is silent and permanent. The tmux
                # runner awaits `communicate()` with no timeout of its own, so a wedged server
                # hangs this coroutine forever -- and the guard below never fires, because
                # nothing is ever raised. The loop simply stops, for the life of the process,
                # with every watched session frozen at whatever it last looked like.
                capture = await asyncio.wait_for(
                    self._capture(record.session_id), timeout=self._capture_timeout
                )
            except Exception:
                # A pane that cannot be read is not a pane that has gone quiet, and this loop
                # runs beside the poll that serves the owner. The watch is left untouched, so
                # a transient failure does not read as a change and re-arm the report.
                _LOG.warning("could not capture a pane while watching for quiet")
                continue
            self._watches[key], activity = observe_quiet(
                self._watches.get(key),
                session_id=key,
                capture=capture,
                now=self._now(),
                quiet_polls=self._quiet_polls,
            )
            if activity is not None:
                activities.append(activity)
        return tuple(activities)
