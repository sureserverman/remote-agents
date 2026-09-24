"""Unit tests for the per-session "a turn started" marker files (BL-108, DEC-104)."""

from __future__ import annotations

import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.turn_markers import FileTurnMarkers


def _markers(tmp_path: Path) -> FileTurnMarkers:
    return FileTurnMarkers(tmp_path / "activity")


def test_a_started_turn_reads_back_with_its_start_time(tmp_path: Path) -> None:
    markers = _markers(tmp_path)
    before = datetime.now(UTC) - timedelta(seconds=1)

    markers.start("s-42")

    started = markers.started_at("s-42")
    assert started is not None
    assert before <= started <= datetime.now(UTC) + timedelta(seconds=1)
    assert markers.sessions() == ("s-42",)


def test_a_second_start_moves_the_time_forward(tmp_path: Path) -> None:
    markers = _markers(tmp_path)
    markers.start("s-42")
    path = tmp_path / "activity" / "turns" / "s-42"
    os.utime(path, (time.time() - 60, time.time() - 60))
    stale = markers.started_at("s-42")

    markers.start("s-42")

    fresh = markers.started_at("s-42")
    assert stale is not None and fresh is not None
    assert fresh - stale > timedelta(seconds=30)


def test_ending_removes_the_marker_and_ending_twice_is_harmless(tmp_path: Path) -> None:
    markers = _markers(tmp_path)
    markers.start("s-42")

    markers.end("s-42")
    markers.end("s-42")
    markers.end("never-started")

    assert markers.started_at("s-42") is None
    assert markers.sessions() == ()


def test_nothing_is_there_before_anything_started(tmp_path: Path) -> None:
    markers = _markers(tmp_path)

    assert markers.started_at("s-42") is None
    assert markers.sessions() == ()
    assert not (tmp_path / "activity").exists()


@pytest.mark.parametrize(
    "session_id", ["../escape", "a/b", "line\nbreak", "", ".", "..", "x" * 300, "s 42"]
)
def test_an_id_that_is_not_a_session_id_writes_nothing(tmp_path: Path, session_id: str) -> None:
    markers = _markers(tmp_path)

    markers.start(session_id)

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == []
    assert markers.started_at(session_id) is None


def test_a_planted_link_at_the_marker_is_not_written_through(tmp_path: Path) -> None:
    markers = _markers(tmp_path)
    markers.start("s-1")
    victim = tmp_path / "victim"
    victim.write_text("keep me", encoding="utf-8")
    old = time.time() - 3600
    os.utime(victim, (old, old))
    (tmp_path / "activity" / "turns" / "s-42").symlink_to(victim)

    markers.start("s-42")

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert victim.stat().st_mtime == pytest.approx(old, abs=1)
    assert markers.started_at("s-42") is None


def test_a_planted_link_in_place_of_the_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "activity").mkdir(mode=0o700)
    (tmp_path / "activity" / "turns").symlink_to(elsewhere)
    markers = _markers(tmp_path)

    markers.start("s-42")

    assert list(elsewhere.iterdir()) == []
    assert markers.started_at("s-42") is None


def test_the_directory_and_each_marker_are_owner_only(tmp_path: Path) -> None:
    markers = _markers(tmp_path)

    markers.start("s-42")

    turns = tmp_path / "activity" / "turns"
    assert stat.S_IMODE(turns.stat().st_mode) == 0o700
    marker = turns / "s-42"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert marker.read_bytes() == b""


def test_a_directory_that_cannot_be_written_answers_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    (tmp_path / "activity").write_text("not a directory", encoding="utf-8")
    markers = _markers(tmp_path)

    markers.start("s-42")
    markers.end("s-42")

    assert markers.started_at("s-42") is None
    assert markers.sessions() == ()


def test_sessions_lists_only_marker_files_with_session_names(tmp_path: Path) -> None:
    markers = _markers(tmp_path)
    markers.start("s-1")
    markers.start("s-2")
    turns = tmp_path / "activity" / "turns"
    (turns / "subdir").mkdir()
    (turns / ".hidden").write_bytes(b"")

    assert sorted(markers.sessions()) == ["s-1", "s-2"]
