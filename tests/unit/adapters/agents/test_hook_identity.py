"""Unit tests for naming the session a hook event belongs to from its own payload (DEC-126)."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.hook_identity import (
    ROSTER_DIRECTORY,
    RosterEntry,
    read_roster,
    resolve,
    roster_path,
    thread_created_at,
    write_roster,
)

#: The engineering-skills thread from the 2026-10-13 incident, and the drill home's thread.
_INCIDENT_THREAD = "01a1360a-d366-7913-abdc-27aa12faf338"
_DRILL_THREAD = "01a1368c-f87e-7f70-8ae6-3923ac339630"


def _thread_at(moment: datetime) -> str:
    """A version-7 id whose time is `moment`, the shape Codex 0.162.1 was measured minting."""
    milliseconds = int(moment.timestamp() * 1000)
    head = f"{milliseconds:012x}"
    return f"{head[:8]}-{head[8:]}-7abc-8def-0123456789ab"


def _entry(
    session: str, workspace: Path, created_at: datetime, resumed: str | None = None
) -> RosterEntry:
    return RosterEntry(session, str(workspace), created_at, resumed)


def test_the_measured_thread_ids_decode_to_the_moments_they_were_minted() -> None:
    assert thread_created_at(_INCIDENT_THREAD) == datetime(
        2026, 10, 13, 16, 14, 1, 574000, tzinfo=UTC
    )
    assert thread_created_at(_DRILL_THREAD) == datetime(
        2026, 10, 13, 18, 36, 10, 750000, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "value",
    [
        "f4020001-e712-4832-9fc8-dd28d38d5b8a",  # version 4: no time in it
        "01a1360a-d366-7913-0bdc-27aa12faf338",  # version 7 digit, wrong variant
        "01a1360ad3667913abdc27aa12faf338",  # no dashes
        "not-a-thread",
        "",
        None,
        42,
    ],
)
def test_an_id_that_is_not_a_version_7_uuid_has_no_time(value: object) -> None:
    assert thread_created_at(value) is None


def test_a_resumed_thread_resolves_to_its_session_before_any_time_rule(tmp_path: Path) -> None:
    started = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    roster = (
        _entry("fresh", tmp_path, started),
        # Resumed later in the same folder, so the time rule alone would pick it for anything new
        # -- but its conversation began long before, and only the exact id can say so.
        _entry("resumed", tmp_path, started + timedelta(hours=1), _INCIDENT_THREAD),
    )

    assert resolve(_INCIDENT_THREAD, str(tmp_path), roster) == "resumed"


def test_two_sessions_in_one_folder_each_keep_their_own_thread(tmp_path: Path) -> None:
    first = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    second = first + timedelta(minutes=30)
    roster = (_entry("first", tmp_path, first), _entry("second", tmp_path, second))

    assert resolve(_thread_at(first + timedelta(seconds=2)), str(tmp_path), roster) == "first"
    assert resolve(_thread_at(second + timedelta(seconds=2)), str(tmp_path), roster) == "second"


def test_a_thread_in_another_folder_is_nobodys(tmp_path: Path) -> None:
    started = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    roster = (_entry("in-a", tmp_path / "a", started),)

    assert resolve(_thread_at(started + timedelta(seconds=1)), str(tmp_path / "b"), roster) is None


def test_the_incident_daemon_case_names_the_pane_that_asked(tmp_path: Path) -> None:
    """The obsidian-wiki session ended and is off the roster; the asking pane is on it."""
    (tmp_path / "engineering-skills").mkdir()
    roster = (
        _entry(
            "8b4f34c9-92c9-45db-bb45-1ab0c24dd7bb",
            tmp_path / "engineering-skills",
            datetime(2026, 10, 13, 16, 14, 0, 387761, tzinfo=UTC),
        ),
    )

    assert (
        resolve(_INCIDENT_THREAD, str(tmp_path / "engineering-skills"), roster)
        == "8b4f34c9-92c9-45db-bb45-1ab0c24dd7bb"
    )


def test_a_symlinked_folder_resolves_to_the_session_in_its_target(tmp_path: Path) -> None:
    started = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    roster = (_entry("real", real.resolve(), started),)

    assert (
        resolve(_thread_at(started + timedelta(seconds=1)), str(tmp_path / "link"), roster)
        == "real"
    )


@pytest.mark.parametrize("thread, cwd", [(None, "/tmp"), (_INCIDENT_THREAD, None), (7, "/tmp")])
def test_a_payload_missing_its_thread_or_folder_is_nobodys(thread: object, cwd: object) -> None:
    roster = (_entry("any", Path("/tmp"), datetime(2026, 1, 1, tzinfo=UTC)),)

    assert resolve(thread, cwd, roster) is None


def test_the_roster_lives_beside_the_turn_markers_not_among_the_records(tmp_path: Path) -> None:
    assert roster_path(tmp_path, "codex") == tmp_path / ROSTER_DIRECTORY / "codex.json"
    # The drain reads only top-level `*.json`, so a roster there would be read as a record.
    assert roster_path(tmp_path, "codex").parent != tmp_path


def test_a_written_roster_reads_back_as_written_and_is_private(tmp_path: Path) -> None:
    started = datetime(2026, 10, 13, 16, 14, 0, 387761, tzinfo=UTC)
    entries = (
        _entry("8b4f34c9-92c9-45db-bb45-1ab0c24dd7bb", tmp_path, started),
        _entry("resumed-1", tmp_path, started, _INCIDENT_THREAD),
    )
    path = roster_path(tmp_path / "activity", "codex")

    write_roster(path, entries)

    assert read_roster(path) == entries
    assert path.stat().st_mode & 0o777 == 0o600
    assert [entry.name for entry in path.parent.iterdir()] == ["codex.json"]


def test_an_empty_roster_is_written_and_reads_as_empty_not_absent(tmp_path: Path) -> None:
    """An empty roster still means the service has spoken: no session is anybody's."""
    path = roster_path(tmp_path, "codex")

    write_roster(path, ())

    assert read_roster(path) == ()


def test_an_absent_roster_is_none(tmp_path: Path) -> None:
    assert read_roster(roster_path(tmp_path, "codex")) is None


def _roster_bytes(**overrides: str) -> bytes:
    entry = {"session_id": "a", "workspace": "/w", "created_at": "2026-01-01T00:00:00+00:00"}
    return json.dumps({"sessions": [{**entry, **overrides}]}).encode("utf-8")


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"sessions": "x"}',
        _roster_bytes(session_id="a b"),  # not an opaque id
        b'{"sessions": [{"session_id": "a", "workspace": "/w", "created_at": "yesterday"}]}',
        _roster_bytes(created_at="2026-01-01T00:00:00"),  # no zone
        b"[" * 200_000,  # nested past the parser's depth: `RecursionError`, not `ValueError`
    ],
)
def test_a_malformed_roster_names_nobody(tmp_path: Path, content: bytes) -> None:
    path = roster_path(tmp_path, "codex")
    path.parent.mkdir()
    path.write_bytes(content)
    path.chmod(0o600)

    assert read_roster(path) == ()


def test_a_roster_others_can_read_names_nobody(tmp_path: Path) -> None:
    path = roster_path(tmp_path, "codex")
    write_roster(path, (_entry("a", tmp_path, datetime(2026, 1, 1, tzinfo=UTC)),))
    path.chmod(0o644)

    assert read_roster(path) == ()


def test_a_symlinked_roster_names_nobody(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"sessions": []}))
    real.chmod(0o600)
    path = roster_path(tmp_path, "codex")
    path.parent.mkdir()
    os.symlink(real, path)

    assert read_roster(path) == ()


def test_writing_a_roster_makes_every_missing_directory_private(tmp_path: Path) -> None:
    """The spool directory may not exist yet; the hooks refuse one that is not owner-only."""
    path = roster_path(tmp_path / "state" / "activity", "codex")

    write_roster(path, ())

    for directory in (tmp_path / "state", tmp_path / "state" / "activity", path.parent):
        assert directory.stat().st_mode & 0o777 == 0o700, directory


def test_a_roster_is_never_written_through_a_linked_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    spool = tmp_path / "activity"
    spool.mkdir(mode=0o700)
    (spool / ROSTER_DIRECTORY).symlink_to(elsewhere)

    with pytest.raises(OSError):
        write_roster(roster_path(spool, "codex"), ())

    assert list(elsewhere.iterdir()) == []


# -- Stage 1 gate remediation: the cases the time rule cannot decide --


def test_a_resumed_thread_is_claimed_even_without_a_folder(tmp_path: Path) -> None:
    """An oversized payload may lose its folder to the prefix cut; the exact id needs none."""
    roster = (_entry("resumed", tmp_path, datetime(2026, 10, 13, tzinfo=UTC), _INCIDENT_THREAD),)

    assert resolve(_INCIDENT_THREAD, None, roster) == "resumed"


def test_an_older_thread_reopened_inside_the_only_session_in_its_folder_is_that_sessions(
    tmp_path: Path,
) -> None:
    """Codex's own `/resume` of an older thread inside a managed pane: too old for the time rule,
    but only one live session works in that folder."""
    started = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    roster = (_entry("only", tmp_path, started),)

    assert resolve(_thread_at(started - timedelta(days=3)), str(tmp_path), roster) == "only"


def test_a_thread_id_with_no_time_goes_to_the_only_session_in_its_folder(tmp_path: Path) -> None:
    """A Codex that stopped minting version-7 ids must not silence every notification."""
    roster = (_entry("only", tmp_path, datetime(2026, 10, 13, tzinfo=UTC)),)

    assert resolve("f4020001-e712-4832-9fc8-dd28d38d5b8a", str(tmp_path), roster) == "only"


def test_an_undecidable_thread_in_a_shared_folder_is_nobodys(tmp_path: Path) -> None:
    """Two sessions in one folder and no usable time: guessing would mis-file, so nobody."""
    started = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    roster = (_entry("first", tmp_path, started), _entry("second", tmp_path, started))

    assert resolve("f4020001-e712-4832-9fc8-dd28d38d5b8a", str(tmp_path), roster) is None
    assert resolve(_thread_at(started - timedelta(days=1)), str(tmp_path), roster) is None


def test_a_relative_folder_names_nobody(tmp_path: Path) -> None:
    started = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
    roster = (_entry("only", Path(os.path.realpath(".")), started),)

    assert resolve(_thread_at(started + timedelta(seconds=1)), ".", roster) is None


def test_the_boundary_is_inclusive_a_thread_minted_in_its_sessions_millisecond(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 10, 13, 16, 0, 0, 123000, tzinfo=UTC)
    later = started + timedelta(minutes=5)
    roster = (_entry("first", tmp_path, started), _entry("second", tmp_path, later))

    assert resolve(_thread_at(started), str(tmp_path), roster) == "first"


def test_a_roster_too_large_for_the_hook_to_read_is_refused_at_write(tmp_path: Path) -> None:
    """Every hook would read an oversized roster as naming nobody; refuse to write one."""
    entries = [
        _entry(f"s{index}", tmp_path / ("x" * 200), datetime(2026, 1, 1, tzinfo=UTC))
        for index in range(2000)
    ]

    with pytest.raises(ValueError):
        write_roster(roster_path(tmp_path, "codex"), entries)


def test_a_pipe_where_the_roster_belongs_names_nobody_without_blocking(tmp_path: Path) -> None:
    """The hook runs in every Codex turn, and opening a FIFO for reading waits for a writer.

    A pipe is not the service's file, so it names nobody -- but only if the open returns: a
    blocking open would hang every Codex hook on the host until something wrote to it.
    """
    path = roster_path(tmp_path, "codex")
    path.parent.mkdir(mode=0o700)
    os.mkfifo(path, 0o600)
    answer: list[object] = []
    reader = threading.Thread(target=lambda: answer.append(read_roster(path)), daemon=True)
    reader.start()
    reader.join(timeout=5.0)
    if reader.is_alive():
        # Release the blocked open so the thread ends, then fail on what was measured.
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(timeout=5.0)
        pytest.fail("reading a roster that is a pipe blocked until a writer came")
    assert answer == [()]


def test_turn_marks_cannot_establish_fresh_thread_ownership(tmp_path):
    from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileTurnMarkers

    roster = (_entry("managed", tmp_path / "source", datetime(2026, 1, 1, tzinfo=UTC)),)
    FileConversationMarks(tmp_path).record("managed", _DRILL_THREAD)
    FileTurnMarkers(tmp_path).start("managed", _DRILL_THREAD)
    assert (
        resolve(_DRILL_THREAD, None, roster, require_exact=True, activity_directory=tmp_path)
        is None
    )
    # A stale conversation mark alone is insufficient after the turn ends.
    FileTurnMarkers(tmp_path).end("managed")
    assert (
        resolve(
            _DRILL_THREAD, str(tmp_path), roster, require_exact=True, activity_directory=tmp_path
        )
        is None
    )


def test_a_new_conversation_cannot_reuse_the_original_resume_binding(tmp_path):
    from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileTurnMarkers

    roster = (_entry("managed", tmp_path, datetime(2026, 1, 1, tzinfo=UTC), _INCIDENT_THREAD),)
    FileConversationMarks(tmp_path).record("managed", _DRILL_THREAD)
    FileTurnMarkers(tmp_path).start("managed", _DRILL_THREAD)
    assert (
        resolve(
            _INCIDENT_THREAD, str(tmp_path), roster, require_exact=True, activity_directory=tmp_path
        )
        is None
    )
    assert (
        resolve(
            _DRILL_THREAD, "/different", roster, require_exact=True, activity_directory=tmp_path
        )
        is None
    )
