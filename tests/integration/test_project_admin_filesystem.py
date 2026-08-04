"""Integration tests wiring project creation to the real filesystem and registry."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from remote_agents.adapters.projects import registry_writer
from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.project_catalog import build_catalogue

_REGISTRY = """version: 1
projects:
  - path: /tmp/remote-agents-existing
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


@pytest.fixture
def dev_root(tmp_path: Path) -> Path:
    root = tmp_path / "dev"
    (root / "infra").mkdir(parents=True)
    (root / "dev-area").mkdir(parents=True)
    (root / "archive").mkdir(parents=True)
    (root / ".hidden").mkdir(parents=True)
    return root


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "projects-registry.yaml"
    path.write_text(_REGISTRY, encoding="utf-8")
    return path


def _service(dev_root: Path, registry_path: Path) -> ProjectCreationService:
    return ProjectCreationService(
        FilesystemProjectWorkspace(dev_root),
        RegistryProjectRecorder(registry_path, dev_root, today=lambda: date(2026, 8, 4)),
    )


def test_created_project_exists_on_disk_and_reads_back_from_the_registry(
    dev_root: Path, registry_path: Path
) -> None:
    created = _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))

    assert created.path == dev_root / "infra" / "new-project"
    assert created.path.is_dir()
    result = load_registry(registry_path)
    assert result.error is None
    assert (created.path, "new-project", "infra") in [
        (entry.path, entry.name, entry.area) for entry in result.projects
    ]


def test_created_project_reaches_the_catalogue_as_a_registered_entry(
    dev_root: Path, registry_path: Path
) -> None:
    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))

    registry = load_registry(registry_path)
    catalogue = build_catalogue(registry.projects, discover_projects(dev_root))
    entry = next(project for project in catalogue if project.name == "new-project")
    assert entry.group == "Registered"


def test_a_rejected_registration_leaves_no_directory_behind(
    dev_root: Path, registry_path: Path
) -> None:
    service = _service(dev_root, registry_path)
    service.create(CreateProjectCommand("infra", "new-project"))
    (dev_root / "infra" / "new-project").rmdir()
    before = registry_path.read_bytes()

    with pytest.raises(ProjectCreationError):
        service.create(CreateProjectCommand("infra", "new-project"))

    assert not (dev_root / "infra" / "new-project").exists()
    assert registry_path.read_bytes() == before


def test_available_areas_lists_real_directories_without_hidden_or_archived_ones(
    dev_root: Path, registry_path: Path
) -> None:
    assert _service(dev_root, registry_path).available_areas() == ("dev-area", "infra")


def test_creating_the_same_project_twice_refuses_the_second_attempt(
    dev_root: Path, registry_path: Path
) -> None:
    service = _service(dev_root, registry_path)
    service.create(CreateProjectCommand("infra", "new-project"))
    before = registry_path.read_bytes()

    with pytest.raises(ProjectCreationError):
        service.create(CreateProjectCommand("infra", "new-project"))

    assert registry_path.read_bytes() == before


def test_workspace_refuses_to_remove_a_directory_outside_the_development_root(
    dev_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere" / "area" / "project"
    outside.mkdir(parents=True)

    with pytest.raises(ValueError):
        FilesystemProjectWorkspace(dev_root).remove(outside)

    assert outside.is_dir()


def test_workspace_removal_never_follows_a_symlink_swapped_in_after_creation(
    dev_root: Path,
) -> None:
    """A link put in the created directory's place must not redirect rollback at its target."""
    victim = dev_root / "dev-area" / "other-project"
    victim.mkdir(parents=True)
    swapped = dev_root / "infra" / "new-project"
    swapped.symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError):
        FilesystemProjectWorkspace(dev_root).remove(swapped)

    assert victim.is_dir()
    assert swapped.is_symlink()


def test_registration_survives_a_failing_directory_sync_after_the_rename(
    dev_root: Path, registry_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement is already visible, so a late fsync error must not undo the project."""
    real_fsync = os.fsync
    calls = {"count": 0}

    def failing_fsync(descriptor: int) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(registry_writer.os, "fsync", failing_fsync)

    created = _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))

    assert created.path.is_dir()
    assert load_registry(registry_path).error is None
    assert "new-project" in registry_path.read_text(encoding="utf-8")
