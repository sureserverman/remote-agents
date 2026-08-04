"""Unit tests for the append-only portfolio registry writer."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.projects.registry_writer import RegistryWriteError, append_project

_HEADER = "# Portfolio projects registry\n# Edit through the skill.\n"
_EXISTING = """version: 1
projects:
  - path: /tmp/remote-agents-existing
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


def _registry(tmp_path: Path, body: str = _HEADER + _EXISTING) -> Path:
    path = tmp_path / "projects-registry.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _dev_root(tmp_path: Path) -> Path:
    root = tmp_path / "dev"
    root.mkdir(exist_ok=True)
    return root


def _project_directory(tmp_path: Path, area: str = "infra", name: str = "new-project") -> Path:
    directory = _dev_root(tmp_path) / area / name
    directory.mkdir(parents=True)
    return directory


def _append(registry: Path, tmp_path: Path, **overrides: Any) -> Path:
    arguments: dict[str, Any] = {
        "dev_root": _dev_root(tmp_path),
        "name": "new-project",
        "area": "infra",
        "added": date(2026, 8, 4),
    }
    arguments.update(overrides)
    return append_project(registry, **arguments)


def test_append_project_preserves_existing_bytes_as_an_exact_prefix(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    before = registry.read_bytes()
    project = _project_directory(tmp_path)

    _append(registry, tmp_path, project_path=project)

    after = registry.read_bytes()
    assert after.startswith(before)
    assert after != before


def test_append_project_writes_exactly_the_closed_entry_schema(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path)

    _append(registry, tmp_path, project_path=project)

    appended = registry.read_text(encoding="utf-8")[len(_HEADER + _EXISTING) :]
    assert appended == (
        f"  - path: {project}\n"
        "    name: new-project\n"
        "    area: infra\n"
        "    enabled: true\n"
        "    added: 2026-08-04\n"
    )


def test_append_project_output_round_trips_through_the_reader(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path)

    _append(registry, tmp_path, project_path=project)

    result = load_registry(registry)
    assert result.error is None
    assert (project.resolve(), "new-project", "infra") in [
        (entry.path, entry.name, entry.area) for entry in result.projects
    ]


def test_append_project_expands_user_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(tmp_path)
    home = tmp_path / "home"
    project = home / "dev" / "infra" / "new-project"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    _append(
        registry,
        tmp_path,
        dev_root=Path("~/dev"),
        project_path=Path("~/dev/infra/new-project"),
    )

    assert "~" not in registry.read_text(encoding="utf-8")
    assert load_registry(registry).error is None


def test_append_project_rejects_a_project_outside_the_configured_dev_root(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    outside = tmp_path / "elsewhere" / "infra" / "new-project"
    outside.mkdir(parents=True)
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=outside)

    assert registry.read_bytes() == unchanged


def test_append_project_rejects_a_project_nested_deeper_than_one_area(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    nested = _dev_root(tmp_path) / "big-projects" / "infra" / "new-project"
    nested.mkdir(parents=True)

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=nested)


def test_append_project_rejects_a_dev_root_that_does_not_exist(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path)

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, dev_root=tmp_path / "missing-root", project_path=project)


def test_append_project_rejects_a_duplicate_canonical_path(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path)
    _append(registry, tmp_path, project_path=project)
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project)

    assert registry.read_bytes() == unchanged


def test_append_project_rejects_a_path_already_held_by_a_disabled_entry(tmp_path: Path) -> None:
    project = _project_directory(tmp_path)
    registry = _registry(
        tmp_path,
        "version: 1\nprojects:\n"
        f"  - path: {project}\n"
        "    name: new-project\n"
        "    area: infra\n"
        "    enabled: false\n"
        "    added: 2026-07-30\n",
    )
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project)

    assert registry.read_bytes() == unchanged


def test_append_project_refuses_a_registry_holding_a_relative_disabled_path(tmp_path: Path) -> None:
    """A disabled entry skips the reader's absolute-path rule; dedup must not resolve it by cwd."""
    project = _project_directory(tmp_path)
    registry = _registry(
        tmp_path,
        "version: 1\nprojects:\n"
        "  - path: some/relative/dir\n"
        "    name: relative\n"
        "    area: infra\n"
        "    enabled: false\n"
        "    added: 2026-07-30\n",
    )
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project)

    assert registry.read_bytes() == unchanged


def test_append_project_refuses_a_registry_entry_without_a_usable_path(tmp_path: Path) -> None:
    project = _project_directory(tmp_path)
    registry = _registry(
        tmp_path,
        "version: 1\nprojects:\n"
        "  - path: 12345\n"
        "    name: numeric\n"
        "    area: infra\n"
        "    enabled: false\n"
        "    added: 2026-07-30\n",
    )

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project)


@pytest.mark.parametrize(
    ("name", "area"),
    [
        ("Not-A-Slug", "infra"),
        ("has space", "infra"),
        ("trailing-", "infra"),
        ("", "infra"),
        ("../escape", "infra"),
        ("new-project", "Infra"),
        ("new-project", "in fra"),
        ("new-project", ""),
    ],
)
def test_append_project_rejects_names_and_areas_outside_the_slug_shape(
    tmp_path: Path, name: str, area: str
) -> None:
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path)
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project, name=name, area=area)

    assert registry.read_bytes() == unchanged


def test_append_project_rejects_an_ancestor_segment_unsafe_as_a_plain_scalar(
    tmp_path: Path,
) -> None:
    """A colon or space in an ancestor directory would break the appended YAML block."""
    registry = _registry(tmp_path)
    unsafe_root = tmp_path / "weird: root"
    project = unsafe_root / "infra" / "new-project"
    project.mkdir(parents=True)
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, dev_root=unsafe_root, project_path=project)

    assert registry.read_bytes() == unchanged


def test_append_project_rejects_a_path_that_contradicts_its_name_and_area(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path, area="infra", name="new-project")

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project, name="different-name")


def test_append_project_rejects_an_area_directory_that_does_not_exist(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    missing = _dev_root(tmp_path) / "infra" / "new-project"

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=missing)


def test_append_project_rejects_a_project_directory_that_does_not_exist(tmp_path: Path) -> None:
    """Registration follows creation; an entry may never point at an absent directory."""
    registry = _registry(tmp_path)
    area = _dev_root(tmp_path) / "infra"
    area.mkdir(parents=True)

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=area / "new-project")


def test_append_project_rejects_a_project_path_that_is_a_regular_file(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    area = _dev_root(tmp_path) / "infra"
    area.mkdir(parents=True)
    (area / "new-project").write_text("", encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=area / "new-project")


def test_append_project_rejects_a_relative_path(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=Path("dev/infra/new-project"))


def test_append_project_refuses_to_extend_a_degraded_registry(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "version: 2\nprojects: []\n")
    project = _project_directory(tmp_path)
    unchanged = registry.read_bytes()

    with pytest.raises(RegistryWriteError):
        _append(registry, tmp_path, project_path=project)

    assert registry.read_bytes() == unchanged


def test_append_project_preserves_the_registry_file_mode(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.chmod(0o600)
    project = _project_directory(tmp_path)

    _append(registry, tmp_path, project_path=project)

    assert stat.S_IMODE(registry.stat().st_mode) == 0o600


def test_append_project_writes_through_a_symlinked_registry_path(tmp_path: Path) -> None:
    """A dotfiles-managed registry is usually a symlink; replacing the link orphans the source."""
    target = tmp_path / "dotfiles" / "projects-registry.yaml"
    target.parent.mkdir()
    target.write_text(_HEADER + _EXISTING, encoding="utf-8")
    link = tmp_path / "projects-registry.yaml"
    link.symlink_to(target)
    project = _project_directory(tmp_path)

    _append(link, tmp_path, project_path=project)

    assert link.is_symlink()
    assert "new-project" in target.read_text(encoding="utf-8")


def test_append_project_terminates_an_unterminated_final_line_before_appending(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _HEADER + _EXISTING.rstrip("\n"))
    project = _project_directory(tmp_path)

    _append(registry, tmp_path, project_path=project)

    assert load_registry(registry).error is None
    assert "added: 2026-07-30\n  - path:" in registry.read_text(encoding="utf-8")


def test_append_project_keeps_every_entry_from_successive_writers(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    first = _project_directory(tmp_path, name="first-project")
    second = _project_directory(tmp_path, name="second-project")

    _append(registry, tmp_path, project_path=first, name="first-project")
    _append(registry, tmp_path, project_path=second, name="second-project")

    result = load_registry(registry)
    assert result.error is None
    assert {entry.name for entry in result.projects} == {
        "existing",
        "first-project",
        "second-project",
    }


def test_append_project_blocks_while_another_writer_holds_the_lock(tmp_path: Path) -> None:
    """Contention, not just sequence: a held lock must stall the second writer until release."""
    registry = _registry(tmp_path)
    project = _project_directory(tmp_path)
    lock_path = registry.resolve().with_name(registry.name + ".lock")
    finished = threading.Event()

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    worker = threading.Thread(
        target=lambda: (_append(registry, tmp_path, project_path=project), finished.set())
    )
    worker.start()
    try:
        assert not finished.wait(timeout=0.5)
        assert "new-project" not in registry.read_text(encoding="utf-8")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert finished.wait(timeout=5)
    worker.join(timeout=5)
    assert "new-project" in registry.read_text(encoding="utf-8")


def test_append_project_locks_beside_the_real_file_for_a_symlinked_registry(
    tmp_path: Path,
) -> None:
    target = tmp_path / "dotfiles" / "projects-registry.yaml"
    target.parent.mkdir()
    target.write_text(_HEADER + _EXISTING, encoding="utf-8")
    link = tmp_path / "projects-registry.yaml"
    link.symlink_to(target)
    project = _project_directory(tmp_path)

    _append(link, tmp_path, project_path=project)

    assert (target.parent / (target.name + ".lock")).exists()
    assert not (link.parent / (link.name + ".lock")).exists()
