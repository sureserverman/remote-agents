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
import os
import re
import time
from collections.abc import Awaitable, Callable, Collection, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from remote_agents.domain.models import SessionId, SessionState
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    ActivitySource,
    AgentActivity,
    activity_source_for,
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

# Observed in the managed Codex TUI while a native local approval is open.  The project name
# after the separator varies, so the predicate admits that suffix while refusing ordinary pane
# titles.  This is terminal metadata, not an upstream hook contract: it may become unavailable
# in a later Codex release, in which case it safely yields no inferred activity.
_CODEX_ACTION_REQUIRED_TITLE = "[ ! ] Action Required | "

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
        pending = [
            *activity_directory.glob(".pending-*.tmp"),
            # A drain's claim, orphaned by a crash between the rename and the read — the
            # same age rule applies: at most one record lost, never one delivered twice.
            *activity_directory.glob(".claim-*.tmp"),
        ]
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

    **The claim comes before everything.** Rename is atomic within one filesystem, so of any
    number of drains racing over this record exactly one owns it; the losers meet an OSError
    and skip, which is how two passes -- a second service instance, an operator's manual run,
    any future second drainer -- can never turn one observation into two notifications or two
    feed rows. The claim's name is invisible to the drain glob, and one orphaned by a crash
    is swept by `_clear_abandoned_temporaries` on the same age rule as a pending temporary:
    at most one record lost, never one delivered twice.
    """
    claimed = path.with_name(f".claim-{uuid4().hex}.tmp")
    with suppress(OSError):
        # Before the rename, not after: the rename preserves the record's mtime, and the
        # abandoned-claim sweep judges claims by mtime — so claiming an hour-old record
        # (a backlog after an outage) would exist, for an instant, as a claim already
        # past the sweep's horizon, and a concurrent drainer's sweep in that instant
        # would eat a live claim. Freshened first, the claim is born its own age. A lost
        # utime race with another drainer's rename costs nothing: our rename then fails
        # and we skip, which is the ordinary losing-the-claim path.
        os.utime(path)
    try:
        path.rename(claimed)
    except OSError:
        return None
    path = claimed
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
    if event == "PermissionRequest":
        return ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED
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


def observe_codex_action_required(
    was_action_required: bool,
    *,
    session_id: str,
    title: str,
    now: datetime,
) -> tuple[bool, AgentActivity | None]:
    """Fold one content-free Codex pane-title observation into its edge state.

    Codex's native command escalation currently bypasses its ``PermissionRequest`` hook.
    Its terminal title nevertheless changes to the fixed marker above, which tmux exposes
    separately from pane capture.  Retaining the title would be terminal-content retention;
    retaining one boolean answers the whole question this observation can safely support.
    """
    action_required = title.startswith(_CODEX_ACTION_REQUIRED_TITLE)
    if not action_required or was_action_required:
        return action_required, None
    return True, AgentActivity(
        session_id=session_id,
        kind=ActivityKind.NEEDS_ANSWER,
        detail=None,
        observed_at=now,
        confidence=ActivityConfidence.INFERRED,
    )


class CodexApprovalWatcher:
    """Watch Codex panes for the one approval its own hook never reports.

    Application-layer under DEC-001: the terminal is reached through a callable the caller
    supplies, so nothing here knows what tmux is, and the driver adapter never reaches a pane.

    Narrowed from `PaneQuietWatcher` on 2026-08-30. That class did two jobs: it hashed a pane
    capture per poll to infer that an agent had gone quiet, and it read a pane *title* to infer
    that Codex had opened a native approval. The first was retired with `ActivityKind.QUIET` --
    it told the owner nothing they could act on, and it was the only thing here that ever
    touched pane content. The second is the whole of DEC-063 and is unchanged.

    So this now reads titles and nothing else, for `ActivitySource.HYBRID` sessions and nothing
    else. It holds one boolean per session -- whether the exact marker was present last pass --
    and never a title, a capture, a command, a prompt, a path or a provider identifier.
    """

    def __init__(
        self,
        store: SessionStore,
        title: Callable[[SessionId], Awaitable[str]],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._title = title
        self._title_timeout = _CAPTURE_TIMEOUT_SECONDS
        self._now = now
        # A title is never stored: only whether the exact native Codex marker was present on
        # the last successful pass. A separate boolean presence set tells a first title read from
        # a known-title recovery without retaining title content.
        self._action_required: dict[str, bool] = {}
        self._title_observed: set[str] = set()
        self._reported_needs_answer_session_ids: frozenset[str] = frozenset()

    def mark_needs_answer_reported(self, session_ids: Collection[str]) -> None:
        """Let this pass prefer a same-pass provider permission over the title edge."""
        self._reported_needs_answer_session_ids = frozenset(session_ids)

    async def poll(self) -> tuple[AgentActivity, ...]:
        """Take one look at every running session whose provider can escalate past its hook."""
        reported_needs_answer_session_ids = self._reported_needs_answer_session_ids
        self._reported_needs_answer_session_ids = frozenset()
        records = await self._store.list((SessionState.RUNNING,))
        watched = [
            record
            for record in records
            if activity_source_for(str(record.profile_id)) is ActivitySource.HYBRID
        ]
        # `is HYBRID`, not `is not HOOK_EXCLUSIVE`. The old predicate also swept in every
        # `UNOBSERVED` profile, which was right while a pane digest was the fallback for
        # exactly those profiles and is wrong now that the only thing read here is a Codex
        # marker: an `opencode` session under the old test would cost a tmux round trip per
        # pass to read a title that can never carry it.
        # Sessions that have gone away keep no state. Without this the maps grow for the life
        # of the service, one entry per session ever launched.
        live = {str(record.session_id) for record in watched}
        self._action_required = {
            key: value for key, value in self._action_required.items() if key in live
        }
        self._title_observed.intersection_update(live)

        activities = []
        for record in watched:
            key = str(record.session_id)
            try:
                # Bounded, because the failure it prevents is silent and permanent. The tmux
                # runner awaits `communicate()` with no timeout of its own, so a wedged server
                # hangs this coroutine forever -- and the guard below never fires, because
                # nothing is ever raised. The loop simply stops, for the life of the process,
                # with every watched session frozen at whatever it last looked like.
                title = await asyncio.wait_for(
                    self._title(record.session_id), timeout=self._title_timeout
                )
            except Exception:
                # A stale positive marker must not indefinitely silence the only signal there
                # is. Re-arm the edge: title metadata cannot tell whether the old local prompt
                # cleared and a new one opened while it was unavailable. A generic recovery
                # notice can duplicate the old prompt, but silently missing the new one would
                # strand the owner with no actionable alert.
                self._action_required[key] = False
                _LOG.warning("could not read a Codex pane title while watching activity")
                continue
            had_successful_title = key in self._title_observed
            action_required, activity = observe_codex_action_required(
                self._action_required.get(key, False),
                session_id=key,
                title=title,
                now=self._now(),
            )
            self._action_required[key] = action_required
            self._title_observed.add(key)
            # A provider-reported permission in the same pass is stronger evidence of the same
            # wait.  Remember the marker but do not make the owner hear it twice; a clear title
            # re-arms the next native prompt.
            if (
                activity is not None
                and had_successful_title
                and key not in reported_needs_answer_session_ids
            ):
                activities.append(activity)
        return tuple(activities)
