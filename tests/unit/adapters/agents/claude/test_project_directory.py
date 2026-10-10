"""Claude files a conversation under `~/.claude/projects/<its cwd, every non-alphanumeric
character as "-">` -- not only "/" (BL-116).

Measured on this host 2026-10-10: of 388 working directories `~/.claude.json` records, every one
whose transcript directory exists is named by that rule, and none by "/" alone. The pairs below
are real ones from that listing. A worktree under `~/.local/state/...` always has a dot, so the
slash-only rule found no worktree conversation and no worktree session's usage at all.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.claude.paths import project_directory_name
from remote_agents.adapters.agents.claude.sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.agent_usage import UsageQuery

_OBSERVED = (
    (
        "/home/user/.cache/ra-drill-20260919/workspace",
        "-home-user--cache-ra-drill-20260919-workspace",
    ),
    ("/home/user/.cache/trace-g_xbfc4w/workspace", "-home-user--cache-trace-g-xbfc4w-workspace"),
    (
        "/tmp/pytest-of-user/pytest-433/test_a_real_claude_approval_sp0/workspace",
        "-tmp-pytest-of-user-pytest-433-test-a-real-claude-approval-sp0-workspace",
    ),
)


@pytest.mark.parametrize(("cwd", "directory"), _OBSERVED)
def test_the_directory_is_the_one_claude_wrote(cwd: str, directory: str) -> None:
    assert project_directory_name(Path(cwd)) == directory


def _dotted(tmp_path: Path) -> Path:
    """A folder shaped like a session worktree: a dot and an underscore on the way."""
    folder = tmp_path / ".local" / "state" / "remote_agents" / "wt"
    folder.mkdir(parents=True)
    return folder


def _claude_directory(root: Path, cwd: Path) -> Path:
    # Spelled out from the measured rule rather than through the code under test.
    name = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(cwd.resolve()))
    directory = root / name
    directory.mkdir(parents=True)
    return directory


@pytest.mark.asyncio
async def test_a_conversation_under_a_dotted_folder_is_listed(tmp_path: Path) -> None:
    folder = _dotted(tmp_path)
    root = tmp_path / "projects"
    conversation = "00000000-0000-4000-8000-0000000000aa"
    (_claude_directory(root, folder) / f"{conversation}.jsonl").write_text("{}\n")
    catalogue = ClaudeSessionCatalogue({ProjectId("p"): folder}, sessions_root=root)

    page = await catalogue.list_conversations(
        profile_id=None, project_id=ProjectId("p"), page=1, page_size=10
    )

    assert len(page.conversations) == 1


def test_a_dotted_workspaces_transcript_is_the_one_read(tmp_path: Path) -> None:
    folder = _dotted(tmp_path)
    root = tmp_path / "projects"
    conversation = "00000000-0000-4000-8000-0000000000bb"
    transcript = _claude_directory(root, folder) / f"{conversation}.jsonl"
    transcript.write_text("{}\n")
    reader = ClaudeUsageReader(sessions_root=root)

    found = reader._transcript_for(
        UsageQuery(ProfileId("claude"), folder, datetime(2026, 10, 10, tzinfo=UTC), conversation)
    )

    assert found == transcript
    assert os.sep not in project_directory_name(folder)
