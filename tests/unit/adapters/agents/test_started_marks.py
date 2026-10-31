"""The started mark, under the turn markers' rules: nothing planted is trusted, nothing raises."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from remote_agents.adapters.agents.turn_markers import FileStartedMarks

_SESSION = "f4020001-e712-4832-9fc8-dd28d38d5b8a"


def _private_directory(activity: Path) -> Path:
    directory = activity / "started"
    directory.mkdir(parents=True, mode=0o700)
    return directory


def test_a_mark_exists_lists_and_can_be_forgotten(tmp_path: Path) -> None:
    marks = FileStartedMarks(tmp_path)
    assert not marks.exists(_SESSION)

    marks.mark(_SESSION)
    marks.mark(_SESSION)

    assert marks.exists(_SESSION)
    assert marks.sessions() == (_SESSION,)
    assert (tmp_path / "started" / _SESSION).read_bytes() == b""

    marks.forget(_SESSION)
    assert not marks.exists(_SESSION)
    assert marks.sessions() == ()


def test_a_fifo_planted_at_the_name_neither_hangs_nor_reads_as_a_mark(tmp_path: Path) -> None:
    os.mkfifo(_private_directory(tmp_path) / _SESSION)
    marks = FileStartedMarks(tmp_path)

    writer = threading.Thread(target=marks.mark, args=(_SESSION,), daemon=True)
    writer.start()
    writer.join(5.0)

    assert not writer.is_alive(), "a planted FIFO hung the hook inside the owner's session"
    assert stat.S_ISFIFO((tmp_path / "started" / _SESSION).lstat().st_mode)
    assert not marks.exists(_SESSION)
    assert marks.sessions() == ()


def test_a_fifo_planted_as_the_directory_reads_as_no_mark(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "started")
    marks = FileStartedMarks(tmp_path)

    marks.mark(_SESSION)
    marks.forget(_SESSION)

    assert not marks.exists(_SESSION)
    assert marks.sessions() == ()


def test_a_link_does_not_read_as_a_mark(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    (_private_directory(tmp_path) / _SESSION).symlink_to(target)
    marks = FileStartedMarks(tmp_path)

    assert not marks.exists(_SESSION)
    assert marks.sessions() == ()


def test_a_malformed_name_is_refused_everywhere(tmp_path: Path) -> None:
    marks = FileStartedMarks(tmp_path)

    marks.mark("../escape")
    marks.forget("../escape")

    assert not marks.exists("../escape")
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "started").exists()


def test_sessions_ignores_a_name_that_is_not_a_session(tmp_path: Path) -> None:
    directory = _private_directory(tmp_path)
    (directory / ".pending-abc").write_bytes(b"")

    assert FileStartedMarks(tmp_path).sessions() == ()
