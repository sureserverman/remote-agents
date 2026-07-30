"""Safety and depth tests for development-root discovery."""

from pathlib import Path

from remote_agents.adapters.projects.discovery import discover_projects


def make_project(root: Path, area: str, project: str) -> Path:
    path = root / area / project
    path.mkdir(parents=True)
    return path


def test_discovery_accepts_exactly_two_directory_levels(tmp_path: Path) -> None:
    make_project(tmp_path, "infra", "remote-agents")
    make_project(tmp_path, "infra", "remote-agents/nested")

    projects = discover_projects(tmp_path)

    assert [(project.area, project.name) for project in projects] == [("infra", "remote-agents")]


def test_discovery_excludes_hidden_archived_ignored_and_files(tmp_path: Path) -> None:
    make_project(tmp_path, "infra", "visible")
    make_project(tmp_path, ".hidden-area", "hidden")
    make_project(tmp_path, "infra", ".hidden-project")
    make_project(tmp_path, "archive", "old")
    make_project(tmp_path, "infra", "archives")
    (tmp_path / "infra" / "not-a-directory").write_text("file", encoding="utf-8")

    projects = discover_projects(tmp_path, ignored_names=("archive", "archives"))

    assert [project.name for project in projects] == ["visible"]


def test_discovery_accepts_a_symlink_that_remains_inside_the_root(tmp_path: Path) -> None:
    target = make_project(tmp_path, "infra", "target")
    (tmp_path / "infra" / "linked").symlink_to(target, target_is_directory=True)

    projects = discover_projects(tmp_path)

    assert {project.name for project in projects} == {"linked", "target"}
    assert {project.path for project in projects} == {target.resolve()}


def test_discovery_rejects_symlink_escape_and_vanished_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-project"
    outside.mkdir(exist_ok=True)
    area = tmp_path / "infra"
    area.mkdir()
    (area / "escape").symlink_to(outside, target_is_directory=True)
    (area / "vanished").symlink_to(tmp_path / "missing", target_is_directory=True)

    assert discover_projects(tmp_path) == ()
