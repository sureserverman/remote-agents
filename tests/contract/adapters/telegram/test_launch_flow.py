"""One composed owner journey from opaque project selection to a typed launch."""

from __future__ import annotations

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.flow import LaunchFlow
from remote_agents.adapters.telegram.projects import CatalogueSnapshot, ProjectNavigator
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject


class FakeLauncher:
    def __init__(self) -> None:
        self.commands = []

    async def launch(self, command) -> None:
        self.commands.append(command)


@pytest.mark.asyncio
async def test_owner_can_complete_the_composed_launch_journey_once() -> None:
    projects = (CatalogProject("a" * 24, "opaque-editor", "writing", "Registered"),)
    profiles = (ProfileAvailability("claude", True),)
    callbacks = CallbackStateStore()
    launcher = FakeLauncher()
    flow = LaunchFlow(
        ProjectNavigator(lambda: CatalogueSnapshot(projects), callbacks, page_size=20),
        lambda: profiles,
        callbacks,
        launcher,
    )

    view = flow.browse_projects("Registered", owner_id=7, chat_id=11, view_revision=1)
    assert flow.select_project(
        view.items[0].callback_token, owner_id=7, chat_id=11, view_revision=1
    )
    choice = flow.profile_choices(owner_id=7, chat_id=11, view_revision=2)[0]
    assert choice.callback_token is not None
    assert flow.select_profile(choice.callback_token, owner_id=7, chat_id=11, view_revision=2)
    flow.set_label("draft", owner_id=7, chat_id=11)
    preview = flow.preview(owner_id=7, chat_id=11, view_revision=3)

    assert await flow.submit(preview.callback_token, owner_id=7, chat_id=11, view_revision=3)
    assert len(launcher.commands) == 1
