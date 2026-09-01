from pathlib import Path

import pytest

from remote_agents.adapters.agents.opencode.sessions import OpenCodeSessionCatalogue
from remote_agents.domain.models import ProfileId, ProjectId


class FakeOpenCodeRunner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.limits: list[int] = []

    async def list_sessions(self, limit: int) -> str:
        self.limits.append(limit)
        return self.output


@pytest.mark.asyncio
async def test_opencode_catalogue_accepts_only_json_entries_bound_to_a_known_project(
    tmp_path: Path,
) -> None:
    project_id = ProjectId("remote-agents")
    runner = FakeOpenCodeRunner(
        f'{{"sessions":[{{"id":"open-one","directory":"{tmp_path}","updatedAt":"2026-08-02T10:00:00Z","title":"private"}},{{"id":"other","directory":"/outside","updatedAt":"2026-08-02T11:00:00Z"}}]}}'
    )
    catalogue = OpenCodeSessionCatalogue({project_id: tmp_path}, runner)

    page = await catalogue.list_conversations(
        profile_id=ProfileId("opencode"), project_id=project_id, page=1, page_size=20
    )

    assert len(page.conversations) == 1
    assert page.conversations[0].description == "private"
    assert runner.limits == [250]
    assert await catalogue.resolve_conversation(page.conversations[0].reference) is not None


@pytest.mark.asyncio
async def test_opencode_catalogue_fails_closed_on_non_json_output(tmp_path: Path) -> None:
    page = await OpenCodeSessionCatalogue(
        {ProjectId("remote-agents"): tmp_path}, FakeOpenCodeRunner("not-json")
    ).list_conversations(profile_id=ProfileId("opencode"), project_id=None, page=1, page_size=20)

    assert page.unavailable_reason == "catalogue_unavailable"
