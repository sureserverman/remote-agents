"""Turning what a hook spooled into activity the owner can be told about."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.application.activity import MAXIMUM_DRAIN, drain_activity
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


def test_a_drain_is_bounded_and_leaves_the_rest_for_the_next_pass(tmp_path: Path) -> None:
    """The drain runs on the service's event loop, so one pass cannot be unbounded work."""
    for index in range(MAXIMUM_DRAIN + 5):
        _spool(tmp_path, event="Stop", detail=f"record {index}")

    first = drain_activity(tmp_path)

    assert len(first) == MAXIMUM_DRAIN
    assert len(list(tmp_path.iterdir())) == 5

    assert len(drain_activity(tmp_path)) == 5
    assert list(tmp_path.iterdir()) == []


def test_a_partly_written_record_is_never_seen(tmp_path: Path) -> None:
    """A drain landing mid-write must not read, and then delete, a record still being written."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".pending-abc123.tmp").write_text('{"event": "St', encoding="utf-8")
    _spool(tmp_path, event="Stop", detail="complete")

    (activity,) = drain_activity(tmp_path)

    assert activity.detail == "complete"
    assert (tmp_path / ".pending-abc123.tmp").exists()
