"""Unit tests for the agent hook's private activity spool."""

from __future__ import annotations

import io
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from remote_agents.adapters.agents.activity_spool import (
    MAXIMUM_DETAIL_CHARACTERS,
    MAXIMUM_PAYLOAD_BYTES,
    SESSION_ID_VARIABLE,
    spool_agent_event,
)

_STOP_PAYLOAD: dict[str, Any] = {
    "session_id": "f4020001-e712-4832-9fc8-dd28d38d5b8a",
    "transcript_path": "/home/user/.claude/projects/infra/f4020001.jsonl",
    "cwd": "/tmp/scratch",
    "prompt_id": "eed32c54-420b-475a-a542-35db69c102b6",
    "permission_mode": "auto",
    "effort": {"level": "high"},
    "hook_event_name": "Stop",
    "stop_hook_active": False,
    "last_assistant_message": "probe",
    "background_tasks": [],
    "session_crons": [],
}


def _clock(moment: datetime = datetime(2026, 8, 11, 14, 22, 33, 123456, tzinfo=UTC)):
    return lambda: moment


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "activity"
    directory.mkdir(mode=0o700)
    return directory


def _stream(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _record(directory: Path) -> dict[str, Any]:
    entries = sorted(directory.iterdir())
    assert len(entries) == 1
    return json.loads(entries[0].read_text(encoding="utf-8"))


def _run(stream: io.BytesIO, directory: Path, session_id: str | None = "s-42", **overrides) -> int:
    environment = {} if session_id is None else {SESSION_ID_VARIABLE: session_id}
    arguments = {"now": _clock()}
    arguments.update(overrides)
    return spool_agent_event(
        stream, activity_directory=directory, environment=environment, **arguments
    )


def test_a_stop_payload_writes_exactly_one_private_file_named_by_session_and_time(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_STOP_PAYLOAD), directory) == 0

    entries = sorted(directory.iterdir())
    assert len(entries) == 1
    assert entries[0].name.startswith("s-42-")
    assert "20260811T142233123456Z" in entries[0].name
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600


def test_a_stop_payload_keeps_only_the_fields_the_notification_needs(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    _run(_stream(_STOP_PAYLOAD), directory)

    assert _record(directory) == {
        "session_id": "s-42",
        "event": "Stop",
        "reason": None,
        "detail": "probe",
        "observed_at": "2026-08-11T14:22:33.123456+00:00",
    }


def test_a_stop_payload_never_spools_the_filesystem_layout_it_carries(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    _run(_stream(_STOP_PAYLOAD), directory)

    spooled = sorted(directory.iterdir())[0].read_text(encoding="utf-8")
    assert "transcript_path" not in spooled
    assert "/tmp/scratch" not in spooled


@pytest.mark.parametrize(
    ("payload", "event", "reason", "detail"),
    [
        (
            {"hook_event_name": "StopFailure", "error_type": "rate_limit"},
            "StopFailure",
            "rate_limit",
            None,
        ),
        (
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Claude needs your permission to use Bash",
            },
            "Notification",
            "permission_prompt",
            "Claude needs your permission to use Bash",
        ),
        (
            {"hook_event_name": "SessionEnd", "end_reason": "logout"},
            "SessionEnd",
            "logout",
            None,
        ),
    ],
)
def test_each_hook_event_spools_its_own_discriminating_field(
    tmp_path: Path, payload: dict[str, Any], event: str, reason: str, detail: str | None
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(payload), directory) == 0

    record = _record(directory)
    assert (record["event"], record["reason"], record["detail"]) == (event, reason, detail)


def test_a_detail_line_is_bounded_and_single_lined(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    message = "first line\n\tsecond   line " + "x" * MAXIMUM_DETAIL_CHARACTERS

    _run(_stream(_STOP_PAYLOAD | {"last_assistant_message": message}), directory)

    detail = _record(directory)["detail"]
    assert len(detail) == MAXIMUM_DETAIL_CHARACTERS
    assert detail.startswith("first line second line x")
    assert "\n" not in detail and "\t" not in detail


@pytest.mark.parametrize("session_id", [None, ""])
def test_without_the_session_variable_the_hook_writes_nothing_and_exits_zero(
    tmp_path: Path, session_id: str | None
) -> None:
    directory = _spool(tmp_path)

    assert _run(_stream(_STOP_PAYLOAD), directory, session_id=session_id) == 0

    assert list(directory.iterdir()) == []


@pytest.mark.parametrize(
    "session_id", ["../escape", "a/b", "..", "s\x0042", "s 42", "s\n42", "x" * 200]
)
def test_an_unsafe_session_variable_can_never_become_a_path(
    tmp_path: Path, session_id: str
) -> None:
    directory = _spool(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert _run(_stream(_STOP_PAYLOAD), directory, session_id=session_id) == 0

    assert list(directory.iterdir()) == []
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json at all", b'{"hook_event_name": ', b"[1, 2, 3]", b'"Stop"', b"null"],
)
def test_a_malformed_payload_writes_nothing_and_exits_zero(tmp_path: Path, raw: bytes) -> None:
    directory = _spool(tmp_path)

    assert _run(io.BytesIO(raw), directory) == 0

    assert list(directory.iterdir()) == []


def test_a_payload_without_a_hook_event_name_writes_nothing(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    payload = dict(_STOP_PAYLOAD)
    del payload["hook_event_name"]

    assert _run(_stream(payload), directory) == 0

    assert list(directory.iterdir()) == []


def test_an_oversized_payload_is_neither_spooled_nor_read_unboundedly(tmp_path: Path) -> None:
    directory = _spool(tmp_path)
    oversized = _STOP_PAYLOAD | {"last_assistant_message": "x" * (MAXIMUM_PAYLOAD_BYTES * 4)}
    stream = _stream(oversized)

    assert _run(stream, directory) == 0

    assert list(directory.iterdir()) == []
    assert stream.tell() <= MAXIMUM_PAYLOAD_BYTES + 1


def test_an_unwritable_spool_exits_zero_without_raising(tmp_path: Path) -> None:
    blocked = tmp_path / "activity"
    blocked.write_text("a regular file stands where the spool should be", encoding="utf-8")

    assert _run(_stream(_STOP_PAYLOAD), blocked) == 0


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_a_spool_that_cannot_be_created_exits_zero_without_raising(tmp_path: Path) -> None:
    # A spool the owner merely tightened is not this case: the owner can always widen its own
    # directory again, and the guard does, back to the 0700 the state directory declares. The
    # reachable failure is a spool that cannot be created at all, which is a parent that
    # refuses it.
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        assert _run(_stream(_STOP_PAYLOAD), blocked / "activity") == 0
        assert not (blocked / "activity").exists()
    finally:
        blocked.chmod(0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_a_loosened_spool_is_returned_to_the_declared_mode_before_a_record_lands(
    tmp_path: Path,
) -> None:
    directory = _spool(tmp_path)
    directory.chmod(0o755)

    assert _run(_stream(_STOP_PAYLOAD), directory) == 0

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert len(list(directory.iterdir())) == 1


def test_a_symlinked_spool_is_refused_rather_than_written_through(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    planted = tmp_path / "activity"
    planted.symlink_to(elsewhere, target_is_directory=True)

    assert _run(_stream(_STOP_PAYLOAD), planted) == 0

    assert list(elsewhere.iterdir()) == []


def test_two_events_in_the_same_tick_do_not_overwrite_each_other(tmp_path: Path) -> None:
    directory = _spool(tmp_path)

    _run(_stream(_STOP_PAYLOAD), directory)
    _run(_stream(_STOP_PAYLOAD | {"last_assistant_message": "second"}), directory)

    entries = sorted(directory.iterdir())
    assert len(entries) == 2
    assert {json.loads(entry.read_text(encoding="utf-8"))["detail"] for entry in entries} == {
        "probe",
        "second",
    }
