import pytest

from remote_agents.adapters.agents.cursor.sessions import CursorSessionCatalogue
from remote_agents.domain.models import ProfileId


@pytest.mark.asyncio
async def test_cursor_reports_resume_as_unavailable_without_scraping_its_interactive_picker() -> (
    None
):
    catalogue = CursorSessionCatalogue()

    page = await catalogue.list_conversations(
        profile_id=ProfileId("cursor-agent"), project_id=None, page=1, page_size=20
    )
    capability = (await catalogue.resume_capabilities())[0]

    assert page.conversations == ()
    assert page.unavailable_reason == "structured_catalogue_unavailable"
    assert capability.selected_resume_available is False
