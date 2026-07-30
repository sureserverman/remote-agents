"""Confirmation callbacks create one revalidated typed launch command."""

from __future__ import annotations

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.launch import LaunchConfirmation, LaunchRequest
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject


class FakeLauncher:
    def __init__(self) -> None:
        self.commands = []

    async def launch(self, command) -> None:
        self.commands.append(command)


@pytest.mark.asyncio
async def test_confirmation_revalidates_and_submits_one_typed_command() -> None:
    projects = [CatalogProject("a" * 24, "opaque-editor", "writing", "Registered")]
    profiles = [ProfileAvailability("claude-remote", True)]
    launcher = FakeLauncher()
    confirmation = LaunchConfirmation(
        lambda: tuple(projects), lambda: tuple(profiles), CallbackStateStore(), launcher
    )

    preview = confirmation.preview(
        LaunchRequest("a" * 24, "claude-remote", "план"), owner_id=7, chat_id=11, view_revision=1
    )

    assert preview.project_name == "opaque-editor"
    assert preview.area == "writing"
    assert preview.group == "Registered"
    assert preview.profile_label == "Claude Remote"
    assert preview.label == "план"
    assert await confirmation.submit(
        preview.callback_token, owner_id=7, chat_id=11, view_revision=1
    )
    assert not await confirmation.submit(
        preview.callback_token, owner_id=7, chat_id=11, view_revision=1
    )
    assert [(str(item.project_id), str(item.profile_id)) for item in launcher.commands] == [
        ("a" * 24, "claude-remote")
    ]
    assert launcher.commands[0].label == "план"


@pytest.mark.asyncio
async def test_stale_or_revalidated_request_never_calls_the_application() -> None:
    projects = [CatalogProject("a" * 24, "opaque-editor", "writing", "Registered")]
    profiles = [ProfileAvailability("claude", True)]
    launcher = FakeLauncher()
    confirmation = LaunchConfirmation(
        lambda: tuple(projects), lambda: tuple(profiles), CallbackStateStore(), launcher
    )
    preview = confirmation.preview(
        LaunchRequest("a" * 24, "claude", None), owner_id=7, chat_id=11, view_revision=2
    )
    projects.clear()

    assert not await confirmation.submit(
        preview.callback_token, owner_id=7, chat_id=11, view_revision=2
    )
    assert launcher.commands == []


@pytest.mark.asyncio
async def test_duplicate_names_remain_distinguishable_and_profile_changes_block_submit() -> None:
    projects = [
        CatalogProject("a" * 24, "opaque-editor", "writing", "Registered"),
        CatalogProject("b" * 24, "opaque-editor", "archive", "Unregistered"),
    ]
    profiles = [ProfileAvailability("claude", True)]
    launcher = FakeLauncher()
    confirmation = LaunchConfirmation(
        lambda: tuple(projects), lambda: tuple(profiles), CallbackStateStore(), launcher
    )
    preview = confirmation.preview(
        LaunchRequest("b" * 24, "claude", None), owner_id=7, chat_id=11, view_revision=3
    )
    profiles[0] = ProfileAvailability("claude", False, "version changed")

    assert (preview.project_name, preview.area, preview.group) == (
        "opaque-editor",
        "archive",
        "Unregistered",
    )
    assert not await confirmation.submit(
        preview.callback_token, owner_id=7, chat_id=11, view_revision=3
    )
    assert launcher.commands == []
