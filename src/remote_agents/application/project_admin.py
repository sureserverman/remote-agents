"""Project-creation use case whose only side effects run through typed ports."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from remote_agents.application.errors import ProjectCreationError
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.ports.project_admin import ProjectRegistry, ProjectWorkspace


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
        """Create then register one project, removing the directory if cataloguing fails."""
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
            self._registry.register(identity, path)
        except Exception as error:
            with suppress(OSError, ValueError):
                self._workspace.remove(path)
            raise ProjectCreationError("project could not be catalogued") from error
        return CreatedProject(identity, path)

    def _identity(self, command: CreateProjectCommand) -> ProjectIdentity:
        try:
            return ProjectIdentity(area=command.area, name=command.name)
        except ValueError as error:
            raise ProjectCreationError(str(error)) from error
