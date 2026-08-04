"""Unit tests for the ported project-creation use case."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.domain.projects import ProjectIdentity


class FakeWorkspace:
    """In-memory workspace recording the order of its filesystem effects."""

    def __init__(self, areas: tuple[str, ...] = ("infra", "dev-area"), existing: bool = False):
        self._areas = areas
        self._existing = existing
        self.calls: list[str] = []
        self.create_error: OSError | None = None
        self.remove_error: Exception | None = None

    def areas(self) -> tuple[str, ...]:
        return self._areas

    def exists(self, identity: ProjectIdentity) -> bool:
        return self._existing

    def create(self, identity: ProjectIdentity) -> Path:
        self.calls.append("create")
        if self.create_error is not None:
            raise self.create_error
        return Path("/dev") / identity.area / identity.name

    def remove(self, path: Path) -> None:
        self.calls.append("remove")
        if self.remove_error is not None:
            raise self.remove_error


class FakeRegistry:
    """In-memory registry that can fail exactly like the real writer."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.registered: list[tuple[ProjectIdentity, Path]] = []
        self.calls: list[str] = []

    def register(self, identity: ProjectIdentity, path: Path) -> None:
        self.calls.append("register")
        if self.error is not None:
            raise self.error
        self.registered.append((identity, path))


def _service(workspace: FakeWorkspace, registry: FakeRegistry) -> ProjectCreationService:
    return ProjectCreationService(workspace, registry)


def test_create_makes_the_directory_before_cataloguing_it() -> None:
    workspace, registry = FakeWorkspace(), FakeRegistry()

    created = _service(workspace, registry).create(CreateProjectCommand("infra", "new-project"))

    assert workspace.calls == ["create"]
    assert registry.registered == [(ProjectIdentity("infra", "new-project"), created.path)]
    assert created.identity == ProjectIdentity("infra", "new-project")
    assert created.path == Path("/dev/infra/new-project")


def test_create_removes_the_new_directory_when_cataloguing_fails() -> None:
    workspace = FakeWorkspace()
    registry = FakeRegistry(error=ValueError("registry already holds this canonical path"))

    with pytest.raises(ProjectCreationError):
        _service(workspace, registry).create(CreateProjectCommand("infra", "new-project"))

    assert workspace.calls == ["create", "remove"]
    assert registry.registered == []


@pytest.mark.parametrize(
    "rollback_error",
    [OSError("directory not empty"), ValueError("refusing to remove a symlinked project path")],
)
def test_create_reports_the_original_failure_when_rollback_also_fails(
    rollback_error: Exception,
) -> None:
    """An undeletable directory is unregistered, so the catalogue failure stays the error."""
    workspace = FakeWorkspace()
    workspace.remove_error = rollback_error
    registry = FakeRegistry(error=ValueError("registry rejected the entry"))

    with pytest.raises(ProjectCreationError) as raised:
        _service(workspace, registry).create(CreateProjectCommand("infra", "new-project"))

    assert raised.value.__cause__ is registry.error
    assert workspace.calls == ["create", "remove"]


@pytest.mark.parametrize(
    ("area", "name"),
    [
        ("infra", "Not-A-Slug"),
        ("infra", "has space"),
        ("infra", ".."),
        ("infra", "../escape"),
        ("infra", "nested/name"),
        ("infra", ""),
        ("../escape", "new-project"),
        ("", "new-project"),
    ],
)
def test_create_rejects_identities_outside_the_slug_rule(area: str, name: str) -> None:
    workspace, registry = FakeWorkspace(), FakeRegistry()

    with pytest.raises(ProjectCreationError):
        _service(workspace, registry).create(CreateProjectCommand(area, name))

    assert workspace.calls == []
    assert registry.calls == []


def test_create_rejects_an_area_that_is_not_an_existing_development_root_child() -> None:
    workspace, registry = FakeWorkspace(areas=("infra",)), FakeRegistry()

    with pytest.raises(ProjectCreationError):
        _service(workspace, registry).create(CreateProjectCommand("invented", "new-project"))

    assert workspace.calls == []


def test_create_rejects_a_project_directory_that_already_exists() -> None:
    workspace, registry = FakeWorkspace(existing=True), FakeRegistry()

    with pytest.raises(ProjectCreationError):
        _service(workspace, registry).create(CreateProjectCommand("infra", "new-project"))

    assert workspace.calls == []
    assert registry.calls == []


def test_create_never_catalogues_a_directory_it_could_not_make() -> None:
    workspace, registry = FakeWorkspace(), FakeRegistry()
    workspace.create_error = FileExistsError("raced with another writer")

    with pytest.raises(ProjectCreationError):
        _service(workspace, registry).create(CreateProjectCommand("infra", "new-project"))

    assert registry.calls == []


def test_available_areas_comes_from_the_workspace() -> None:
    workspace, registry = FakeWorkspace(areas=("dev-area", "infra")), FakeRegistry()

    assert _service(workspace, registry).available_areas() == ("dev-area", "infra")
