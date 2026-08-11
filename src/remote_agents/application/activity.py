"""Read what the agent hooks spooled, and say what it means, once.

The spool is the boundary between a hook running inside somebody else's process and this
service. Everything on the far side is untrusted in the ordinary sense -- it may be absent,
truncated, or shaped differently by a newer agent release -- so every step here drops what it
cannot read rather than raising, and a record that reaches no mapping is dropped rather than
guessed at. Reporting the wrong reason an agent stopped is worse than reporting nothing: the
owner acts on these.

Draining deletes. A record turned into activity is gone from disk before it is returned, so a
service that restarts halfway through a delivery cannot tell the owner the same thing twice.
The cost is the opposite failure -- a crash between the delete and the send loses one
notification -- and that is the right way round for a message that says an agent is waiting.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    bounded_detail_line,
)
from remote_agents.ports.session_identity import safe_session_id

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

# Only the reasons that answer "why did it stop". Every other value these fields can take --
# authentication_failed, auth_success, elicitation_*, and whatever upstream adds next -- is
# absent on purpose, and an event carrying one is dropped by the lookup below.
_STOP_FAILURES = {
    "rate_limit": ActivityKind.LIMIT_REACHED,
    "max_output_tokens": ActivityKind.LIMIT_REACHED,
}
_NOTIFICATIONS = {
    "permission_prompt": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED),
    "agent_needs_input": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED),
    "idle_prompt": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.INFERRED),
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
    """
    found = _STAMP.search(path.name)
    return (found.group(0) if found else "", path.name)


def _drained(paths: list[Path]) -> Iterator[AgentActivity]:
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # Broad on purpose, and for the same reason the hook's own boundary is: this is
            # the frame that *deletes*. `json.loads` answers deeply nested input with
            # `RecursionError` and a huge file with `MemoryError`, neither of which is an
            # `OSError` or a `ValueError`, so both escaped, propagated out through this
            # generator, and destroyed a pass whose earlier records had already been
            # unlinked. One unreadable record is worth one lost record, never the batch.
            _LOG.warning("discarding an unreadable activity record")
            record = None
        finally:
            # Before the record is used, not after: a record that is read and then fails to
            # delete would be delivered again on the next pass, and repeating "your agent is
            # waiting" every sixty seconds is the storm this whole path is arranged to avoid.
            path.unlink(missing_ok=True)
        activity = _activity(record) if isinstance(record, dict) else None
        if activity is not None:
            yield activity


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
    if event == "SessionEnd":
        return ActivityKind.ENDED, ActivityConfidence.REPORTED
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
