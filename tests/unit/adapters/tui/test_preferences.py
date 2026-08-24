"""The one preference this surface remembers, and the many ways it may fail to."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from remote_agents.adapters.tui.preferences import (
    ALPHABETICAL,
    DEFAULT_PROJECT_ORDER,
    RECENCY,
    read_project_order,
    write_project_order,
)


def test_the_default_order_is_recency_which_is_what_the_bot_opens_in() -> None:
    assert DEFAULT_PROJECT_ORDER == RECENCY
    assert {RECENCY, ALPHABETICAL} == {"recency", "alphabetical"}


def test_a_written_order_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"

    write_project_order(path, ALPHABETICAL)

    assert read_project_order(path) == ALPHABETICAL


def test_the_preference_file_is_written_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"

    write_project_order(path, ALPHABETICAL)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_loose_mode_on_an_existing_file_is_repaired_by_the_next_write(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)

    write_project_order(path, ALPHABETICAL)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("label", "contents"),
    [
        ("empty", ""),
        ("whitespace", "   \n"),
        ("not json", "alphabetical"),
        ("a json list", "[]"),
        ("a json string", '"alphabetical"'),
        ("no such key", '{"theme": "dark"}'),
        ("an unknown value", '{"project_order": "by-vibes"}'),
        ("a value of the wrong type", '{"project_order": 3}'),
        ("a null value", '{"project_order": null}'),
    ],
)
def test_an_unusable_file_reads_back_as_the_default(
    tmp_path: Path, label: str, contents: str
) -> None:
    """A surface that will not start over a UI preference is worse than one that forgets it.

    Every one of these is a file the owner could produce by hand, by a half-finished write,
    or by a future version writing a value this one does not know. None of them is a reason
    to fail to draw a project list.
    """
    path = tmp_path / "preferences.json"
    path.write_text(contents, encoding="utf-8")

    assert read_project_order(path) == DEFAULT_PROJECT_ORDER, label


def test_an_absent_file_reads_back_as_the_default(tmp_path: Path) -> None:
    assert read_project_order(tmp_path / "nothing-here.json") == DEFAULT_PROJECT_ORDER


def test_an_unwired_path_reads_back_as_the_default() -> None:
    """A host that wired no preferences path forgets the choice; it does not refuse to draw."""
    assert read_project_order(None) == DEFAULT_PROJECT_ORDER


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_file_reads_back_as_the_default(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    write_project_order(path, ALPHABETICAL)
    os.chmod(path, 0o000)

    assert read_project_order(path) == DEFAULT_PROJECT_ORDER


@pytest.mark.skipif(os.getuid() == 0, reason="root writes through a read-only directory")
def test_a_write_failure_is_logged_and_does_not_propagate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    os.chmod(directory, 0o500)
    path = directory / "preferences.json"

    with caplog.at_level(logging.WARNING, logger="remote_agents.adapters.tui.preferences"):
        write_project_order(path, ALPHABETICAL)

    assert not path.exists()
    assert caplog.records, "a preference that could not be saved is worth one line"


def test_an_unwired_path_is_not_written_and_is_not_an_error(tmp_path: Path) -> None:
    write_project_order(None, ALPHABETICAL)

    assert list(tmp_path.iterdir()) == []


def test_the_file_written_is_json_an_owner_can_read(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"

    write_project_order(path, ALPHABETICAL)

    assert json.loads(path.read_text(encoding="utf-8")) == {"project_order": ALPHABETICAL}


def test_a_write_replaces_rather_than_appends(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"

    write_project_order(path, ALPHABETICAL)
    write_project_order(path, RECENCY)

    assert read_project_order(path) == RECENCY
    assert json.loads(path.read_text(encoding="utf-8")) == {"project_order": RECENCY}


def test_writing_an_unknown_order_is_refused_rather_than_stored(tmp_path: Path) -> None:
    """The reader already forgives an unknown value; the writer must not create one."""
    path = tmp_path / "preferences.json"
    write_project_order(path, ALPHABETICAL)

    write_project_order(path, "by-vibes")

    assert read_project_order(path) == ALPHABETICAL
