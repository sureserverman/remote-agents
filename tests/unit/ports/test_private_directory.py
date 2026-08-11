"""A spool directory must not be redirectable by whoever gets there first."""

from __future__ import annotations

import stat
from pathlib import Path

from remote_agents.ports.private_directory import open_private_directory


def test_a_missing_directory_is_created_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "state" / "activity"

    created = open_private_directory(target)

    assert created == target
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_an_existing_directory_is_accepted_and_its_mode_repaired(tmp_path: Path) -> None:
    target = tmp_path / "activity"
    target.mkdir(mode=0o755)

    created = open_private_directory(target)

    assert created == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_a_symlinked_directory_is_refused_rather_than_written_through(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = tmp_path / "activity"
    target.symlink_to(elsewhere, target_is_directory=True)

    assert open_private_directory(target) is None
    assert not any(elsewhere.iterdir())


def test_a_symlinked_parent_is_refused_rather_than_written_through(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    parent = tmp_path / "state"
    parent.symlink_to(elsewhere, target_is_directory=True)

    assert open_private_directory(parent / "activity") is None
    assert not any(elsewhere.iterdir())


def test_a_plain_file_where_the_directory_belongs_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "activity"
    target.write_text("not a directory", encoding="utf-8")

    assert open_private_directory(target) is None
    assert target.read_text(encoding="utf-8") == "not a directory"


def test_a_directory_that_cannot_be_created_is_refused_without_raising(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        assert open_private_directory(blocked / "activity") is None
    finally:
        blocked.chmod(0o700)
