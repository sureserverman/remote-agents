"""The root a session's handoff writer reports, kept as one private file per session.

`remote-agents session-id` writes `<activity>/roots/<session>` when its caller names the git top
level it writes envelopes under (`PLANNING_HANDOFF_ROOT`). The service later reads it to find
that session's envelopes. The write is the conversation marks' atomic private write; the read
refuses anything a tampering process could have left: a link, a file another user owns or
others can read, an oversized one, and content that no longer passes the acceptance rule.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from remote_agents.adapters.agents.reported_roots import ROOTS_DIRECTORY, FileReportedRoots
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.ports.reported_roots import ReportedRoots

_SESSION = "ra-claude_1"


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--template=", str(path)], check=True, timeout=30, capture_output=True
    )
    return Path(os.path.realpath(path))


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "activity"
    directory.mkdir(mode=0o700)
    return directory


def _roots(spool: Path) -> FileReportedRoots:
    return FileReportedRoots(spool, accept=acceptable_root)


def test_the_adapter_satisfies_the_port(tmp_path: Path) -> None:
    roots: ReportedRoots = _roots(_spool(tmp_path))
    assert roots.latest(_SESSION) is None


def test_a_recorded_root_reads_back_as_written(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    top = _repo(tmp_path / "repo")
    roots = _roots(spool)

    assert roots.record(_SESSION, top) is True

    path = spool / ROOTS_DIRECTORY / _SESSION
    assert path.read_bytes() == os.fsencode(top) + b"\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (spool / ROOTS_DIRECTORY).stat().st_mode & 0o777 == 0o700
    assert roots.latest(_SESSION) == top
    assert roots.sessions() == (_SESSION,)


def test_a_newer_record_replaces_the_older(tmp_path: Path) -> None:
    roots = _roots(_spool(tmp_path))
    first, second = _repo(tmp_path / "one"), _repo(tmp_path / "two")

    roots.record(_SESSION, first)
    roots.record(_SESSION, second)

    assert roots.latest(_SESSION) == second


def test_forget_removes_one_session_only(tmp_path: Path) -> None:
    roots = _roots(_spool(tmp_path))
    top = _repo(tmp_path / "repo")
    roots.record(_SESSION, top)
    roots.record("other", top)

    roots.forget(_SESSION)

    assert roots.latest(_SESSION) is None
    assert roots.sessions() == ("other",)


def test_a_root_the_rule_refuses_is_never_written(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    top = _repo(tmp_path / "repo")
    (top / "sub").mkdir()
    roots = _roots(spool)

    assert roots.record(_SESSION, top / "sub") is False
    assert roots.record("../escape", top) is False
    assert not (spool / ROOTS_DIRECTORY).exists() or not any((spool / ROOTS_DIRECTORY).iterdir())


def test_a_write_that_cannot_land_answers_false_without_raising(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    top = _repo(tmp_path / "repo")
    spool.chmod(0o500)
    try:
        assert _roots(spool).record(_SESSION, top) is False
    finally:
        spool.chmod(0o700)


def _planted(tmp_path: Path) -> tuple[Path, Path, FileReportedRoots]:
    spool = _spool(tmp_path)
    top = _repo(tmp_path / "repo")
    roots = _roots(spool)
    assert roots.record(_SESSION, top)
    return spool / ROOTS_DIRECTORY / _SESSION, top, roots


def test_latest_refuses_a_link(tmp_path: Path) -> None:
    path, top, roots = _planted(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(os.fsencode(top) + b"\n")
    elsewhere.chmod(0o600)
    path.unlink()
    path.symlink_to(elsewhere)

    assert roots.latest(_SESSION) is None


def test_latest_refuses_a_group_readable_file(tmp_path: Path) -> None:
    path, _, roots = _planted(tmp_path)
    path.chmod(0o640)

    assert roots.latest(_SESSION) is None


def test_latest_refuses_an_oversized_file(tmp_path: Path) -> None:
    path, top, roots = _planted(tmp_path)
    path.write_bytes(os.fsencode(top) + b"\n" + b"/" * 4096)

    assert roots.latest(_SESSION) is None


def test_latest_refuses_a_file_another_user_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, roots = _planted(tmp_path)
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)

    assert roots.latest(_SESSION) is None


@pytest.mark.parametrize("content", ["sub", "relative", "two-lines", "no-newline", "binary"])
def test_latest_refuses_content_the_rule_refuses(tmp_path: Path, content: str) -> None:
    path, top, roots = _planted(tmp_path)
    (top / "sub").mkdir()
    path.write_bytes(
        {
            "sub": os.fsencode(top / "sub") + b"\n",
            "relative": b"repo\n",
            "two-lines": os.fsencode(top) + b"\n" + os.fsencode(top) + b"\n",
            "no-newline": os.fsencode(top),
            "binary": b"\xff\xfe\n",
        }[content]
    )

    assert roots.latest(_SESSION) is None


def test_latest_never_waits_on_a_fifo(tmp_path: Path) -> None:
    path, _, roots = _planted(tmp_path)
    path.unlink()
    os.mkfifo(path, 0o600)

    assert roots.latest(_SESSION) is None
    assert roots.sessions() == ()


def test_sessions_clears_abandoned_temporaries_only(tmp_path: Path) -> None:
    path, _, roots = _planted(tmp_path)
    old = path.parent / ".pending-old"
    fresh = path.parent / ".pending-fresh"
    old.write_bytes(b"")
    fresh.write_bytes(b"")
    hours_ago = time.time() - 2 * 3600
    os.utime(old, (hours_ago, hours_ago))

    assert roots.sessions() == (_SESSION,)
    assert not old.exists()
    assert fresh.exists()


def test_nothing_recorded_reads_as_nothing(tmp_path: Path) -> None:
    roots = _roots(_spool(tmp_path))

    assert roots.latest(_SESSION) is None
    assert roots.latest("../escape") is None
    assert roots.sessions() == ()
    roots.forget(_SESSION)
