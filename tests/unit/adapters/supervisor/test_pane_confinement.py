"""The AppArmor label of a process, read for `doctor`'s sandbox line (BL-147)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.supervisor.pane_confinement import read_confinement


def _proc_with(tmp_path: Path, pid: int, content: bytes) -> Path:
    attr = tmp_path / str(pid) / "attr"
    attr.mkdir(parents=True)
    (attr / "current").write_bytes(content)
    return tmp_path


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"unprivileged_userns (enforce)\n", "unprivileged_userns (enforce)"),
        (b"unconfined\n", "unconfined"),
        (b"unconfined", "unconfined"),
        (b"unconfined\n\0", "unconfined"),
        (b"snap.codex.codex (complain)\n", "snap.codex.codex (complain)"),
    ],
)
def test_an_apparmor_label_is_read(tmp_path: Path, content: bytes, expected: str) -> None:
    assert read_confinement(4242, proc=_proc_with(tmp_path, 4242, content)) == expected


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"system_u:system_r:unconfined_t:s0\n",  # SELinux: not an AppArmor host
        b"\xff\xfe",
    ],
)
def test_a_label_that_is_not_apparmor_answers_none(tmp_path: Path, content: bytes) -> None:
    assert read_confinement(4242, proc=_proc_with(tmp_path, 4242, content)) is None


def test_a_missing_process_answers_none(tmp_path: Path) -> None:
    assert read_confinement(4242, proc=tmp_path) is None


def test_a_non_positive_pid_answers_none(tmp_path: Path) -> None:
    _proc_with(tmp_path, 0, b"unconfined\n")
    assert read_confinement(0, proc=tmp_path) is None
