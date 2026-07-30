"""Composed owner-only launch journey over the safe Telegram adapter primitives."""

from __future__ import annotations

from collections.abc import Callable

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.launch import LaunchConfirmation, LaunchPreview, LaunchRequest
from remote_agents.adapters.telegram.projects import ProjectNavigator, ProjectView
from remote_agents.adapters.telegram.wizard import LaunchWizard, ProfileAvailability, ProfileChoice


class LaunchFlow:
    """Keep wizard state scoped to the authorized owner/chat rather than globally shared."""

    def __init__(
        self,
        projects: ProjectNavigator,
        profiles: Callable[[], tuple[ProfileAvailability, ...]],
        callbacks: CallbackStateStore,
        launcher: object,
    ) -> None:
        self._projects = projects
        self._profiles = profiles
        self._callbacks = callbacks
        self._launcher = launcher
        self._selected: dict[tuple[int, int], tuple[str, LaunchWizard]] = {}
        self._confirmations: dict[str, LaunchConfirmation] = {}

    def browse_projects(
        self, group: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> ProjectView:
        return self._projects.browse(
            group=group, page=0, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )

    def select_project(
        self, token: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> bool:
        project = self._projects.resolve_selection(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )
        if project is None:
            return False
        self._selected[(owner_id, chat_id)] = (
            project.opaque_id,
            LaunchWizard(self._profiles, self._callbacks),
        )
        return True

    def profile_choices(
        self, *, owner_id: int, chat_id: int, view_revision: int
    ) -> tuple[ProfileChoice, ...]:
        return self._wizard(owner_id, chat_id).profile_choices(
            owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )

    def select_profile(
        self, token: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> bool:
        return (
            self._wizard(owner_id, chat_id).select_profile(
                token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
            )
            is not None
        )

    def set_label(self, label: str | None, *, owner_id: int, chat_id: int) -> str | None:
        return self._wizard(owner_id, chat_id).set_label(label)

    def preview(self, *, owner_id: int, chat_id: int, view_revision: int) -> LaunchPreview:
        project_id, wizard = self._selected[(owner_id, chat_id)]
        if wizard.selected_profile is None:
            raise ValueError("launch profile is not selected")
        project = self._projects.current_project(project_id)
        if project is None:
            raise ValueError("launch project is no longer available")
        confirmation = LaunchConfirmation(
            lambda: tuple(item for item in (self._projects.current_project(project_id),) if item),
            self._profiles,
            self._callbacks,
            self._launcher,
        )
        preview = confirmation.preview(
            LaunchRequest(project_id, wizard.selected_profile, wizard.label),
            owner_id=owner_id,
            chat_id=chat_id,
            view_revision=view_revision,
        )
        self._confirmations[preview.callback_token] = confirmation
        return preview

    async def submit(self, token: str, *, owner_id: int, chat_id: int, view_revision: int) -> bool:
        confirmation = self._confirmations.get(token)
        if confirmation is None:
            return False
        return await confirmation.submit(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )

    def _wizard(self, owner_id: int, chat_id: int) -> LaunchWizard:
        return self._selected[(owner_id, chat_id)][1]
