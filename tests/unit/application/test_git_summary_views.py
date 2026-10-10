"""One git line and one row token for both surfaces (BL-117, DEC-091)."""

from __future__ import annotations

import pytest

from remote_agents.application.session_views import git_line, git_token
from remote_agents.ports.git_checkout import GitSummary


def _summary(
    branch: str | None = "ra/3f2a1b2c",
    files: int = 6,
    added: int = 182,
    removed: int = 74,
    ahead: int = 0,
    dirty: bool = True,
) -> GitSummary:
    return GitSummary(branch, files, added, removed, ahead, dirty)


def test_a_worktree_line_is_its_branch_its_changes_and_how_far_ahead() -> None:
    assert (
        git_line(_summary(ahead=2), shared=False)
        == "ra/3f2a1b2c · 6 files +182 −74 · 2 ahead · dirty"
    )


def test_a_shared_folder_line_says_it_is_the_whole_checkout() -> None:
    assert (
        git_line(_summary(branch="main"), shared=True)
        == "checkout: main · 6 files +182 −74 · dirty"
    )


@pytest.mark.parametrize(
    ("summary", "line"),
    [
        (_summary(files=0, added=0, removed=0, dirty=False), "ra/3f2a1b2c · clean"),
        (_summary(files=1, added=1, removed=0), "ra/3f2a1b2c · 1 file +1 −0 · dirty"),
        (_summary(branch=None, files=0, added=0, removed=0, dirty=False), "detached · clean"),
        (_summary(files=0, added=0, removed=0, dirty=True), "ra/3f2a1b2c · dirty"),
        (
            _summary(files=2, added=3, removed=1, ahead=1, dirty=False),
            "ra/3f2a1b2c · 2 files +3 −1 · 1 ahead",
        ),
    ],
)
def test_each_part_appears_only_when_there_is_something_to_say(
    summary: GitSummary, line: str
) -> None:
    assert git_line(summary, shared=False) == line


def test_the_row_token_counts_changed_files_and_is_empty_when_clean() -> None:
    assert git_token(_summary()) == "±6"
    assert git_token(_summary(files=0, added=0, removed=0, dirty=False)) == ""
    # Untracked files only: changed, though no tracked file is.
    assert git_token(_summary(files=0, added=0, removed=0, dirty=True)) == "±"
