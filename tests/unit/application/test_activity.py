"""Turning what a hook spooled into activity the owner can be told about."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.application import activity as activity_module
from remote_agents.application.activity import (
    MAXIMUM_DRAIN,
    MAXIMUM_RECORD_BYTES,
    drain_activity,
)
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind

_OBSERVED_AT = "2026-08-11T07:30:00+00:00"


def _spool(directory: Path, **fields: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "session_id": "a-session",
        "event": "Stop",
        "reason": None,
        "detail": None,
        "observed_at": _OBSERVED_AT,
        **fields,
    }
    path = directory / f"{record['session_id']}-{len(list(directory.iterdir()))}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_finished_turn_is_completed_and_carries_what_the_agent_last_said(
    tmp_path: Path,
) -> None:
    _spool(tmp_path, event="Stop", detail="Refactored the parser and all tests pass")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.COMPLETED
    assert activity.detail == "Refactored the parser and all tests pass"
    assert activity.session_id == "a-session"
    assert activity.observed_at == datetime(2026, 8, 11, 7, 30, tzinfo=UTC)
    assert activity.confidence is ActivityConfidence.REPORTED


def test_a_rate_limit_is_a_limit_reached(tmp_path: Path) -> None:
    _spool(tmp_path, event="StopFailure", reason="rate_limit")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.LIMIT_REACHED
    assert activity.confidence is ActivityConfidence.REPORTED


def test_an_exhausted_output_budget_is_a_limit_reached(tmp_path: Path) -> None:
    _spool(tmp_path, event="StopFailure", reason="max_output_tokens")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.LIMIT_REACHED


def test_a_permission_request_is_an_agent_that_needs_an_answer(tmp_path: Path) -> None:
    _spool(tmp_path, event="Notification", reason="permission_prompt", detail="Allow Bash?")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.REPORTED
    assert activity.detail == "Allow Bash?"


def test_an_agent_saying_it_needs_input_needs_an_answer(tmp_path: Path) -> None:
    _spool(tmp_path, event="Notification", reason="agent_needs_input")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.REPORTED


def test_an_idle_timer_needs_an_answer_but_only_as_a_guess(tmp_path: Path) -> None:
    """`idle_prompt` is a 60-second timer with recorded false positives and false negatives.

    It is still worth reporting, but never as the same claim as an agent that actually said
    it was waiting, so it carries a confidence the presentation layer can weaken its wording
    from rather than being flattened into the reported kinds.
    """
    _spool(tmp_path, event="Notification", reason="idle_prompt")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.INFERRED


def test_a_session_that_ended_is_ended(tmp_path: Path) -> None:
    _spool(tmp_path, event="SessionEnd", reason="logout")

    (activity,) = drain_activity(tmp_path)

    assert activity.kind is ActivityKind.ENDED


def test_an_event_this_service_does_not_map_is_dropped_rather_than_guessed(
    tmp_path: Path,
) -> None:
    _spool(tmp_path, event="PreToolUse")
    _spool(tmp_path, event="StopFailure", reason="authentication_failed")
    _spool(tmp_path, event="Notification", reason="auth_success")

    assert drain_activity(tmp_path) == ()


def test_every_drained_file_is_gone_so_a_restart_cannot_deliver_it_twice(
    tmp_path: Path,
) -> None:
    _spool(tmp_path, event="Stop", detail="done")
    _spool(tmp_path, event="PreToolUse")

    assert len(drain_activity(tmp_path)) == 1
    assert list(tmp_path.iterdir()) == []
    assert drain_activity(tmp_path) == ()


def test_a_detail_line_is_bounded_and_single_lined(tmp_path: Path) -> None:
    _spool(tmp_path, event="Stop", detail="first line\nsecond line   with  gaps" + "x" * 500)

    (activity,) = drain_activity(tmp_path)

    assert activity.detail is not None
    assert "\n" not in activity.detail
    assert len(activity.detail) <= 240


def test_an_unreadable_record_is_dropped_without_stopping_the_drain(tmp_path: Path) -> None:
    """One bad file must not cost every other activity waiting beside it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "wrong-shape.json").write_text('["a list"]', encoding="utf-8")
    _spool(tmp_path, event="Stop", detail="survived")

    (activity,) = drain_activity(tmp_path)

    assert activity.detail == "survived"
    assert list(tmp_path.iterdir()) == []


def test_activities_arrive_oldest_first(tmp_path: Path) -> None:
    _spool(tmp_path, event="Stop", detail="earlier", observed_at="2026-08-11T07:00:00+00:00")
    _spool(tmp_path, event="Stop", detail="later", observed_at="2026-08-11T09:00:00+00:00")
    _spool(tmp_path, event="Stop", detail="middle", observed_at="2026-08-11T08:00:00+00:00")

    assert [activity.detail for activity in drain_activity(tmp_path)] == [
        "earlier",
        "middle",
        "later",
    ]


def test_a_spool_that_does_not_exist_yet_drains_to_nothing(tmp_path: Path) -> None:
    assert drain_activity(tmp_path / "never-created") == ()


def test_a_timestamp_without_an_offset_costs_only_its_own_record(tmp_path: Path) -> None:
    """A naive timestamp used to take every record in the batch down with it.

    The records are deleted as they are read, so a comparison that raised while sorting them
    destroyed everything already unlinked -- and this module promises to drop what it cannot
    read rather than raise. One foreign writer's bad clock is worth one lost record, never
    every other agent's pending activity.
    """
    _spool(tmp_path, event="Stop", detail="aware", observed_at="2026-08-11T07:30:00+00:00")
    _spool(tmp_path, event="Stop", detail="naive", observed_at="2026-08-11T07:31:00")

    activities = drain_activity(tmp_path)

    assert [activity.detail for activity in activities] == ["aware"]
    assert list(tmp_path.iterdir()) == []


def _spool_as_the_hook_names_it(
    directory: Path, session_id: str, stamp: str, **fields: object
) -> Path:
    """Write a record under the name `_write_privately` actually produces.

    The other helper here names files by a counter, which is fine for tests that only care
    that a record round-trips -- and useless for one about ordering, because the real name is
    `{session_id}-{stamp}.json` and the session comes first.
    """
    directory.mkdir(parents=True, exist_ok=True)
    observed_at = (
        f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}+00:00"
    )
    record = {
        "session_id": session_id,
        "event": "Stop",
        "reason": None,
        "detail": None,
        "observed_at": observed_at,
        **fields,
    }
    path = directory / f"{session_id}-{stamp}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_backlog_is_taken_oldest_first_rather_than_by_whichever_session_sorts_first(
    tmp_path: Path,
) -> None:
    """A bounded drain has to take the *oldest* records, not an arbitrary subset of them.

    The stamp is in the name so the drain can order what it finds, but the session id is in
    front of it, and a session id is a UUID -- so sorting the names sorts by session, and the
    bound then truncates whole sessions rather than the newest records. A session whose id
    happens to sort late would keep losing every pass while a busier one won, and the operator
    would never be told that agent is waiting. Ordering after truncating cannot fix this: by
    then the records that should have been taken are the ones left behind.
    """
    # The stamp is `%Y%m%dT%H%M%S%fZ`, so the varying part has to stay inside the six
    # microsecond digits; widening a field instead would just make an unparseable name.
    for index in range(MAXIMUM_DRAIN):
        _spool_as_the_hook_names_it(
            tmp_path, "aaaa", f"20260811T080000{index:06d}Z", detail=f"new {index}"
        )
    for index in range(5):
        _spool_as_the_hook_names_it(
            tmp_path, "zzzz", f"20260811T070000{index:06d}Z", detail=f"old {index}"
        )

    first = drain_activity(tmp_path)

    assert len(first) == MAXIMUM_DRAIN
    assert [activity.detail for activity in first[:5]] == [f"old {index}" for index in range(5)]
    # What is left behind is the newest, which is the half of the promise that makes the
    # remainder genuinely "the next pass takes it" rather than "that session never wins".
    assert {path.name.split("-")[0] for path in tmp_path.iterdir()} == {"aaaa"}


def test_a_drain_is_bounded_and_leaves_the_rest_for_the_next_pass(tmp_path: Path) -> None:
    """The drain runs on the service's event loop, so one pass cannot be unbounded work."""
    for index in range(MAXIMUM_DRAIN + 5):
        _spool(tmp_path, event="Stop", detail=f"record {index}")

    first = drain_activity(tmp_path)

    assert len(first) == MAXIMUM_DRAIN
    assert len(list(tmp_path.iterdir())) == 5

    assert len(drain_activity(tmp_path)) == 5
    assert list(tmp_path.iterdir()) == []


def test_a_record_that_cannot_be_parsed_at_all_costs_only_its_own_record(tmp_path: Path) -> None:
    """`ValueError` is not the only way `json.loads` refuses, and the batch is already deleted.

    Deeply nested JSON raises `RecursionError`, and a huge file raises `MemoryError`; neither
    is an `OSError` or a `ValueError`, so both escaped the guard, propagated out through the
    generator, and took down a pass whose earlier records had already been unlinked. The spool
    side of this boundary catches broad `Exception` for exactly this reason. The side that
    *deletes* had the narrower guard.
    """
    _spool(tmp_path, event="Stop", detail="before")
    # Deep enough to overflow the decoder's stack with margin. The exact depth that trips it
    # is an interpreter build detail, which is the reason to catch broadly rather than to
    # enumerate the exceptions a parser can raise.
    (tmp_path / "poison.json").write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    _spool(tmp_path, event="Stop", detail="after")

    activities = drain_activity(tmp_path)

    assert sorted(activity.detail or "" for activity in activities) == ["after", "before"]
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_record_costs_only_itself(tmp_path: Path) -> None:
    """The hook bounds what it writes; the drain reads whatever is on disk.

    A foreign writer is explicitly tolerated here, so the 32 KiB cap the hook applies to its
    input says nothing about the size of a file this reads back. Without a bound of its own
    the drain loads the whole thing and hands it to `bounded_detail_line`, whose
    `" ".join(value.split())` expands a large string into millions of objects -- and the
    resulting `MemoryError` escaped a guard that only covered read-and-parse, taking every
    already-unlinked record in the batch with it.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "oversized.json").write_text(
        json.dumps(
            {
                "session_id": "a-session",
                "event": "Stop",
                "reason": None,
                "detail": "x " * MAXIMUM_RECORD_BYTES,
                "observed_at": _OBSERVED_AT,
            }
        ),
        encoding="utf-8",
    )
    _spool(tmp_path, event="Stop", detail="modest")

    activities = drain_activity(tmp_path)

    assert [activity.detail for activity in activities] == ["modest"]
    assert list(tmp_path.iterdir()) == []


def test_a_record_that_fails_after_it_is_read_still_costs_only_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guarding read-and-parse is not the same as guarding the record.

    The mapping runs after the file is unlinked and used to sit outside the guard, so anything
    raising there destroyed the batch just as surely as a parse failure did -- the same class,
    two lines further down. Fixing the first without moving the boundary left the second.
    """
    _spool(tmp_path, event="Stop", detail="before")
    _spool(tmp_path, event="Stop", detail="explodes")
    _spool(tmp_path, event="Stop", detail="after")

    real = activity_module._activity

    def explode_on_one(record: dict):
        if record.get("detail") == "explodes":
            raise MemoryError("simulated")
        return real(record)

    monkeypatch.setattr(activity_module, "_activity", explode_on_one)

    activities = drain_activity(tmp_path)

    assert sorted(a.detail or "" for a in activities) == ["after", "before"]
    assert list(tmp_path.iterdir()) == []


def test_a_session_id_is_bounded_on_the_way_out_as_well_as_in(tmp_path: Path) -> None:
    """The spool constrains this value because it becomes a filename; the drain must too.

    A different process writes these files -- the module says so, and tolerating a foreign
    writer is the design -- so the shape the spool enforces proves nothing about what the
    drain reads back. `detail` is already re-bounded here for that exact reason. This is the
    same argument about the field that reaches the owner as a session's name.
    """
    # Written directly rather than through `_spool`, which names the file after the session
    # id -- these two ids are exactly the ones that cannot be a filename, which is the point.
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, session_id in (
        ("huge", "s" * 4096),
        ("newline", "a-session\nInjected: text"),
    ):
        (tmp_path / f"foreign-{name}.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "event": "Stop",
                    "reason": None,
                    "detail": name,
                    "observed_at": _OBSERVED_AT,
                }
            ),
            encoding="utf-8",
        )
    _spool(tmp_path, event="Stop", detail="fine", session_id="a-session")

    activities = drain_activity(tmp_path)

    assert [activity.detail for activity in activities] == ["fine"]


def test_a_partly_written_record_is_never_seen(tmp_path: Path) -> None:
    """A drain landing mid-write must not read, and then delete, a record still being written."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".pending-abc123.tmp").write_text('{"event": "St', encoding="utf-8")
    _spool(tmp_path, event="Stop", detail="complete")

    (activity,) = drain_activity(tmp_path)

    assert activity.detail == "complete"
    assert (tmp_path / ".pending-abc123.tmp").exists()


def test_an_abandoned_temporary_is_eventually_cleared_and_a_live_one_is_not(
    tmp_path: Path,
) -> None:
    """A hook killed between creating its temporary and linking it leaves the temporary behind.

    Nothing collected them: the drain globs `*.json`, and these are named to be invisible to
    exactly that glob so a half-written record is never read. So they accumulated in the
    owner's spool for the life of the machine, one per killed hook.

    The age threshold is what separates the two cases, because there is no other way to tell
    an abandoned temporary from one being written this instant -- and deleting the second
    would destroy the record the earlier test exists to protect.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    abandoned = tmp_path / ".pending-longgone.tmp"
    abandoned.write_text("half a record", encoding="utf-8")
    os.utime(abandoned, (0, 0))
    live = tmp_path / ".pending-rightnow.tmp"
    live.write_text("being written", encoding="utf-8")

    assert drain_activity(tmp_path) == ()

    assert not abandoned.exists()
    assert live.exists()


def test_a_file_with_no_stamp_drains_first_rather_than_waiting_behind_the_bound(
    tmp_path: Path,
) -> None:
    """The fallback's ordering guarantee, at the scale where it actually decides something.

    `_written_at` gives an unstamped name the empty string, which sorts before every real
    stamp, so a foreign file is always drained rather than accumulating behind a full backlog
    it could never get in front of. Below `MAXIMUM_DRAIN` nothing is being chosen between, so
    only a full backlog distinguishes this from a fallback that merely happens not to crash.
    """
    for index in range(MAXIMUM_DRAIN):
        _spool_as_the_hook_names_it(
            tmp_path, "aaaa", f"20260811T080000{index:06d}Z", detail=f"stamped {index}"
        )
    (tmp_path / "foreign-no-stamp.json").write_text(
        json.dumps(
            {
                "session_id": "a-session",
                "event": "Stop",
                "reason": None,
                "detail": "unstamped",
                "observed_at": _OBSERVED_AT,
            }
        ),
        encoding="utf-8",
    )

    first = drain_activity(tmp_path)

    assert "unstamped" in [activity.detail for activity in first]
    # Exactly one stamped record is displaced, and it is the newest one -- not the foreign
    # file left behind to be reconsidered on every future pass.
    left = [path.name for path in tmp_path.iterdir()]
    assert left == [f"aaaa-20260811T080000{MAXIMUM_DRAIN - 1:06d}Z.json"]
