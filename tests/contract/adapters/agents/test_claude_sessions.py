from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.agents.claude_sessions import (
    ClaudeSessionCatalogue,
    _claude_project_directory,
)
from remote_agents.domain.models import ProfileId, ProjectId


@pytest.mark.asyncio
async def test_claude_catalogue_uses_only_uuid_filenames_and_mtime(tmp_path: Path) -> None:
    project_id = ProjectId("remote-agents")
    project = tmp_path / "project"
    root = tmp_path / "claude-projects"
    directory = root / _claude_project_directory(project)
    directory.mkdir(parents=True)
    first = directory / f"{uuid4()}.jsonl"
    second = directory / f"{uuid4()}.jsonl"
    first.write_text('{"private":"transcript body"}', encoding="utf-8")
    second.write_text('{"private":"other transcript"}', encoding="utf-8")
    first.touch()

    catalogue = ClaudeSessionCatalogue({project_id: project}, sessions_root=root)
    page = await catalogue.list_conversations(
        profile_id=ProfileId("claude"), project_id=project_id, page=1, page_size=1
    )

    assert len(page.conversations) == 1
    assert page.page_count == 2
    assert all("transcript" not in str(summary) for summary in page.conversations)
    assert await catalogue.resolve_conversation(page.conversations[0].reference) is not None


@pytest.mark.asyncio
async def test_claude_catalogue_ignores_non_uuid_filenames_and_fails_closed_for_missing_project(
    tmp_path: Path,
) -> None:
    project_id = ProjectId("remote-agents")
    root = tmp_path / "claude-projects"
    directory = root / _claude_project_directory(tmp_path / "project")
    directory.mkdir(parents=True)
    (directory / "not-a-session.jsonl").write_text("private", encoding="utf-8")
    catalogue = ClaudeSessionCatalogue({project_id: tmp_path / "project"}, sessions_root=root)

    page = await catalogue.list_conversations(
        profile_id=ProfileId("claude"), project_id=project_id, page=1, page_size=20
    )
    missing = await catalogue.list_conversations(
        profile_id=ProfileId("claude"), project_id=ProjectId("opaque-editor"), page=1, page_size=20
    )

    assert page.conversations == ()
    assert missing.unavailable_reason == "project_not_available"


@pytest.mark.asyncio
async def test_claude_capability_reports_unavailable_catalogue_truthfully(tmp_path: Path) -> None:
    capability = (
        await ClaudeSessionCatalogue({}, sessions_root=tmp_path / "missing").resume_capabilities()
    )[0]

    assert capability.catalogue_available is False
    assert capability.selected_resume_available is False
    assert capability.reason == "catalogue_unavailable"
