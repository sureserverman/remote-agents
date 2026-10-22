"""The handoff root is the git top level holding a project, or nothing (DEC-130)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from remote_agents.adapters.workflow.roots import acceptable_root, handoff_root


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


# --- Task 2.2: the one rule a reported root must pass, on write and on read ---


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--template=", str(path)], check=True, timeout=30, capture_output=True
    )
    return path


def test_an_absolute_git_top_level_this_user_owns_is_acceptable(tmp_path: Path) -> None:
    top = _repo(tmp_path / "repo")

    assert acceptable_root(str(top)) == Path(os.path.realpath(top))


def test_a_link_to_a_top_level_is_accepted_as_its_target(tmp_path: Path) -> None:
    top = _repo(tmp_path / "repo")
    link = tmp_path / "link"
    link.symlink_to(top)

    assert acceptable_root(str(link)) == Path(os.path.realpath(top))


@pytest.mark.parametrize("shape", ["subfolder", "no-repo", "relative", "file", "missing"])
def test_anything_but_a_top_level_is_not_acceptable(tmp_path: Path, shape: str) -> None:
    top = _repo(tmp_path / "repo")
    (top / "sub").mkdir()
    (tmp_path / "loose").mkdir()
    (top / "notes.txt").write_text("x", encoding="utf-8")
    value = {
        "subfolder": str(top / "sub"),
        "no-repo": str(tmp_path / "loose"),
        "relative": os.path.relpath(top),
        "file": str(top / "notes.txt"),
        "missing": str(tmp_path / "gone"),
    }[shape]

    assert acceptable_root(value) is None


@pytest.mark.parametrize("value", [None, "", 7, "/tmp/\0nul"])
def test_a_value_that_names_no_path_is_not_acceptable(value: object) -> None:
    assert acceptable_root(value) is None


def test_a_top_level_another_user_owns_is_not_acceptable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top = _repo(tmp_path / "repo")
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)

    assert acceptable_root(str(top)) is None


@pytest.mark.parametrize("shape", ["empty-file", "not-a-gitfile", "link-to-a-repo"])
def test_a_reported_root_needs_a_real_git_marker(tmp_path: Path, shape: str) -> None:
    """A tool shell chooses a reported root, so `.git` must be what git itself would read: a
    directory, or a worktree's `gitdir:` file. A link or a file of anything else is refused,
    though `handoff_root`'s presence test, which sets a session's default root, still takes it."""
    root = tmp_path / "root"
    root.mkdir()
    marker = root / ".git"
    if shape == "empty-file":
        marker.write_text("")
    elif shape == "not-a-gitfile":
        marker.write_text("not a gitfile\n")
    else:
        real = tmp_path / "real"
        subprocess.run(["git", "init", "-q", "--template=", str(real)], check=True, timeout=60)
        marker.symlink_to(real / ".git")

    assert handoff_root(root) == root
    assert acceptable_root(str(root)) is None


def test_a_worktree_gitdir_file_is_a_real_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

    assert acceptable_root(str(tmp_path)) == Path(os.path.realpath(tmp_path))
