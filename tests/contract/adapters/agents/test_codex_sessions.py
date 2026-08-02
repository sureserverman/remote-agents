from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex_sessions import CodexAppServerClient, CodexSessionCatalogue
from remote_agents.domain.models import ProfileId, ProjectId


class FakeThreadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str | None, int]] = []

    async def list_threads(self, *, cwd: Path, cursor: str | None, limit: int) -> dict[str, object]:
        self.calls.append((cwd, cursor, limit))
        if cursor is None:
            return {
                "data": [
                    {"id": "thread-one", "updatedAt": 1_754_127_200, "name": "private"}
                ],
                "nextCursor": "second-page",
            }
        return {
            "data": [
                {"id": "thread-two", "updatedAt": 1_754_130_800, "name": "secret"},
                {"id": "bad source id", "updatedAt": 1_754_134_400},
                {"id": "overflow", "updatedAt": 10**100},
            ]
        }


@pytest.mark.asyncio
async def test_codex_catalogue_feature_probes_thread_list_with_bounded_pagination(
    tmp_path: Path,
) -> None:
    project_id = ProjectId("remote-agents")
    client = FakeThreadClient()
    catalogue = CodexSessionCatalogue({project_id: tmp_path}, client)

    page = await catalogue.list_conversations(
        profile_id=ProfileId("codex"), project_id=project_id, page=1, page_size=20
    )

    assert len(page.conversations) == 2
    assert page.conversations[0].updated_at.isoformat() == "2025-08-02T10:33:20+00:00"
    assert all(
        "private" not in str(summary) and "secret" not in str(summary)
        for summary in page.conversations
    )
    assert client.calls == [(tmp_path, None, 50), (tmp_path, "second-page", 50)]
    assert await catalogue.resolve_conversation(page.conversations[0].reference) is not None


@pytest.mark.asyncio
async def test_codex_catalogue_fails_closed_on_invalid_protocol_shape(tmp_path: Path) -> None:
    class InvalidClient:
        async def list_threads(self, **_kwargs: object) -> dict[str, object]:
            return {"data": "not-a-list"}

    page = await CodexSessionCatalogue(
        {ProjectId("remote-agents"): tmp_path}, InvalidClient()
    ).list_conversations(profile_id=ProfileId("codex"), project_id=None, page=1, page_size=20)

    assert page.conversations == ()
    assert page.unavailable_reason == "catalogue_unavailable"


@pytest.mark.asyncio
async def test_codex_app_server_client_uses_only_thread_list_and_bounded_arguments(
    tmp_path: Path,
) -> None:
    class RecordingProtocol:
        def __init__(self) -> None:
            self.call: tuple[str, dict[str, object]] | None = None

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            self.call = (method, params)
            return {"data": []}

    protocol = RecordingProtocol()
    client = CodexAppServerClient(protocol)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="reviewed bound"):
        await client.list_threads(cwd=tmp_path, cursor=None, limit=51)

    assert protocol.call is None
