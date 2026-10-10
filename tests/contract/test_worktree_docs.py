"""The README's and runbook's worktree sections say what the code does (BL-116, BL-117).

The README's `[worktrees]` example is parsed through the real loader, so a key `config.py`
does not accept, or a path it refuses, fails here rather than on an owner's `serve`. The kept
reasons, the default copy list and the branch prefix are read off the code, not restated.
"""

from __future__ import annotations

import re
from pathlib import Path

from remote_agents.application.worktree_cleanup import (
    DETACHED_BASE,
    DIRTY,
    IN_USE,
    MOVED,
    REMOVABLE,
    UNMERGED,
)
from remote_agents.application.worktrees import BRANCH_PREFIX
from remote_agents.config import DEFAULT_WORKTREE_COPY, describe_schema_drift, load_config

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    level = heading.split(" ", 1)[0]
    following = re.search(rf"\n{re.escape(level)} ", text[start + len(heading) + 2 :])
    end = len(text) if following is None else start + len(heading) + 2 + following.start()
    return text[start:end]


WORKTREES = _section(README, "## Worktrees")
RUNBOOK_WORKTREES = _section(RUNBOOK, "## Session worktrees")


def test_the_readme_worktrees_example_loads_through_the_config_loader(tmp_path: Path) -> None:
    (example,) = re.findall(r"```toml\n(.*?)```", WORKTREES, flags=re.DOTALL)
    path = tmp_path / "config.toml"
    path.write_text(
        f'''[paths]
dev_root = "{tmp_path}"
registry_path = "{tmp_path}/registry.yaml"
database_path = "{tmp_path}/sessions.sqlite3"

[limits]
max_label_length = 40
project_page_size = 10
activity_poll_seconds = 30

{example}''',
        encoding="utf-8",
    )

    config = load_config(path)

    drift = describe_schema_drift(path)
    assert not drift.get("unknown") and not drift.get("missing") and not drift.get("invalid"), drift
    assert "docs/local-notes.md" in config.worktree_copy
    project = Path("/home/you/dev/web")
    assert config.worktree_copy_for(project) == ("CLAUDE.md", "docs/local-notes.md")
    assert config.worktree_copy_for(Path("/elsewhere")) == config.worktree_copy


def test_the_readme_names_the_default_copy_list_and_the_branch() -> None:
    for entry in DEFAULT_WORKTREE_COPY:
        assert f"`{entry}`" in WORKTREES, entry
    assert f"`{BRANCH_PREFIX}<first 8 of the session id>`" in WORKTREES


def test_every_kept_reason_doctor_can_print_is_documented() -> None:
    for reason in (UNMERGED, MOVED, DIRTY, IN_USE, DETACHED_BASE):
        assert f"`{reason}`" in WORKTREES, reason
        assert f"`{reason}`" in RUNBOOK_WORKTREES, reason
    assert f"`{REMOVABLE}`" in RUNBOOK_WORKTREES


def test_the_runbook_links_the_trust_measurement_that_exists() -> None:
    (link,) = re.findall(r"\((acceptance-2026-10-09-worktree-trust\.md)\)", RUNBOOK_WORKTREES)
    assert (ROOT / "docs" / link).is_file()
    assert "(../README.md#worktrees)" in RUNBOOK_WORKTREES
