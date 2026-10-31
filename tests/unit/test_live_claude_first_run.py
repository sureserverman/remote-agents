"""A drill passes Claude's first-run setup only with the owner's opt-in, in its own home."""

from __future__ import annotations

import json
from pathlib import Path

import native_isolation
import pytest
from native_isolation import claude_command


@pytest.fixture
def real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "real"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/.credentials.json").write_text("{}")
    (home / ".claude.json").write_text('{"owner": true}')
    monkeypatch.setenv("HOME", str(home))
    # Builds a command and runs nothing, so a host without bwrap can still check the seed.
    monkeypatch.setattr(native_isolation.shutil, "which", lambda _name: "/usr/bin/bwrap")
    return home


def test_first_run_is_left_alone_by_default(
    tmp_path: Path, real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REMOTE_AGENTS_LIVE_CLAUDE_FIRST_RUN", raising=False)
    root = tmp_path / "drill"
    claude_command(Path("/bin/true"), root)
    assert not (root / ".claude.json").exists()


def test_the_opt_in_marks_only_the_disposable_home(
    tmp_path: Path, real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_CLAUDE_FIRST_RUN", "owner-approved")
    root = tmp_path / "drill"
    claude_command(Path("/bin/true"), root)
    seeded = json.loads((root / ".claude.json").read_text())
    assert seeded["hasCompletedOnboarding"] is True
    assert seeded["hasSeenAutoModeOutsideReadPrompt"] is True
    assert (real_home / ".claude.json").read_text() == '{"owner": true}'
