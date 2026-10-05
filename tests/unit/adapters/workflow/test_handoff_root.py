"""The handoff root is the git top level holding a project, or nothing (coder-plugins DEC-029)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.workflow.roots import handoff_root


def test_a_project_at_the_top_level_is_its_own_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    assert handoff_root(tmp_path) == tmp_path


def test_a_project_in_a_subdirectory_shares_its_checkouts_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    project = tmp_path / "packages" / "editor"
    project.mkdir(parents=True)

    assert handoff_root(project) == tmp_path


def test_a_worktree_marks_its_top_level_with_a_git_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

    assert handoff_root(tmp_path / "sub") == tmp_path


def test_a_project_in_no_checkout_has_no_root(tmp_path: Path) -> None:
    project = tmp_path / "loose"
    project.mkdir()
    if any((parent / ".git").exists() for parent in tmp_path.parents):
        pytest.skip("the temporary directory sits inside a checkout on this host")

    assert handoff_root(project) is None
    assert handoff_root(None) is None
