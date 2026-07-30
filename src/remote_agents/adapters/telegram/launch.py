"""Typed, replay-safe Telegram launch confirmation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import ProfileId, ProjectId


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    project_opaque_id: str
    profile_id: str
    label: str | None


@dataclass(frozen=True, slots=True)
class LaunchPreview:
    project_name: str
    profile_label: str
    label: str | None
    callback_token: str


class LaunchConfirmation:
    """Re-resolve a pending launch request before exactly one application submission."""

    def __init__(
        self,
        projects: Callable[[], tuple[CatalogProject, ...]],
        profiles: Callable[[], tuple[ProfileAvailability, ...]],
        callbacks: CallbackStateStore,
        launcher: object,
    ) -> None:
        self._projects = projects
        self._profiles = profiles
        self._callbacks = callbacks
        self._launch: Callable[[LaunchCommand], Awaitable[object]] = launcher.launch
        self._requests: dict[str, LaunchRequest] = {}

    def preview(
        self, request: LaunchRequest, *, owner_id: int, chat_id: int, view_revision: int
    ) -> LaunchPreview:
        project, profile = self._resolve_request(request)
        if project is None or profile is None:
            raise ValueError("launch request is no longer available")
        token = self._callbacks.create(
            "launch.confirm",
            request.project_opaque_id,
            owner_id,
            chat_id,
            view_revision,
            mutation=True,
        )
        self._requests[token] = request
        return LaunchPreview(project.name, _profile_label(profile.profile_id), request.label, token)

    async def submit(self, token: str, *, owner_id: int, chat_id: int, view_revision: int) -> bool:
        request = self._requests.get(token)
        if request is None or not self._callbacks.claim_mutation(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        ):
            return False
        project, profile = self._resolve_request(request)
        if project is None or profile is None:
            return False
        await self._launch(
            LaunchCommand(
                ProjectId(project.name), ProfileId(profile.profile_id), token, request.label
            )
        )
        return True

    def _resolve_request(
        self, request: LaunchRequest
    ) -> tuple[CatalogProject | None, ProfileAvailability | None]:
        project = next(
            (item for item in self._projects() if item.opaque_id == request.project_opaque_id), None
        )
        profile = next(
            (
                item
                for item in self._profiles()
                if item.profile_id == request.profile_id and item.available
            ),
            None,
        )
        return project, profile


def _profile_label(profile_id: str) -> str:
    return {
        "claude": "Claude",
        "claude-remote": "Claude Remote",
        "codex": "Codex",
        "opencode": "OpenCode",
        "cursor-agent": "Cursor Agent",
    }[profile_id]
