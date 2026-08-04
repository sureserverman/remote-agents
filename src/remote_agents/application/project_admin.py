"""Project-creation use case whose only side effects run through typed ports."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from remote_agents.application.errors import ProjectCreationError
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.ports.project_admin import ProjectRegistry, ProjectWorkspace

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreateProjectCommand:
    """Owner-supplied request to create and catalogue one project."""

    area: str
    name: str


@dataclass(frozen=True, slots=True)
class CreatedProject:
    """A project directory that exists and is recorded in the registry."""

    identity: ProjectIdentity
    path: Path


class ProjectCreationService:
    """Create a project directory and catalogue it, or leave the host unchanged."""

    def __init__(self, workspace: ProjectWorkspace, registry: ProjectRegistry) -> None:
        self._workspace = workspace
        self._registry = registry

    def available_areas(self) -> tuple[str, ...]:
        """Return the existing areas a new project may be created in."""
        return self._workspace.areas()

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        """Create then register one project, removing the directory if cataloguing fails.

        This performs blocking filesystem and lock work; an asyncio caller must run it off
        the event loop.
        """
        identity = self._identity(command)
        if identity.area not in self._workspace.areas():
            raise ProjectCreationError("area is not an existing development-root directory")
        if self._workspace.exists(identity):
            raise ProjectCreationError("project directory already exists")
        try:
            path = self._workspace.create(identity)
        except OSError as error:
            raise ProjectCreationError("project directory could not be created") from error
        try:
            recorded = self._registry.register(identity, path)
        except Exception as error:
            self._roll_back(path)
            raise ProjectCreationError("project could not be catalogued") from error
        return CreatedProject(identity, recorded)

    def _roll_back(self, path: Path) -> None:
        """Undo the created directory, recording the rare case where it cannot be undone."""
        try:
            self._workspace.remove(path)
        except (OSError, ValueError):
            _LOG.warning("left an uncatalogued project directory behind after a failed create")

    def _identity(self, command: CreateProjectCommand) -> ProjectIdentity:
        try:
            return ProjectIdentity(area=command.area, name=command.name)
        except ValueError as error:
            raise ProjectCreationError(str(error)) from error
