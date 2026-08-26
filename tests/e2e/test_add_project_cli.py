"""The add-project command creates one project and refuses to duplicate it."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.projects.registry import load_registry
from remote_agents.bootstrap import main


def workspace(tmp_path: Path) -> tuple[Path, Path]:
    dev_root = tmp_path / "dev"
    (dev_root / "infra" / "seed").mkdir(parents=True)
    registry_path = tmp_path / "projects-registry.yaml"
    registry_path.write_text(
        "version: 1\n"
        "projects:\n"
        f"  - path: {dev_root / 'infra' / 'seed'}\n"
        "    name: seed\n"
        "    area: infra\n"
        "    enabled: true\n"
        "    added: 2026-01-01\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[paths]
dev_root = "{dev_root}"
registry_path = "{registry_path}"
database_path = "{tmp_path}/sessions.sqlite3"

[limits]
max_label_length = 40
project_page_size = 10
activity_poll_seconds = 30
activity_quiet_polls = 3
''',
        encoding="utf-8",
    )
    return config_path, registry_path


def test_add_project_cli_creates_the_directory_and_registers_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, registry_path = workspace(tmp_path)
    created = tmp_path / "dev" / "infra" / "widget"
    argv = ["add-project", "--config", str(config_path), "--area", "infra", "--name", "widget"]

    status = main(argv)

    assert status == 0
    assert created.is_dir()
    assert capsys.readouterr().out.strip() == str(created)
    registered = load_registry(registry_path).projects
    assert [project.path for project in registered] == [
        tmp_path / "dev" / "infra" / "seed",
        created,
    ]


def test_add_project_cli_rejects_a_duplicate_without_changing_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, registry_path = workspace(tmp_path)
    argv = ["add-project", "--config", str(config_path), "--area", "infra", "--name", "widget"]
    assert main(argv) == 0
    recorded = registry_path.read_bytes()
    capsys.readouterr()

    status = main(argv)

    captured = capsys.readouterr()
    assert status != 0
    assert "already exists" in captured.err
    assert captured.out == ""
    assert registry_path.read_bytes() == recorded


def test_add_project_cli_refuses_a_path_the_registry_already_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The registry's own duplicate guard is reachable once the directory is gone."""
    config_path, registry_path = workspace(tmp_path)
    argv = ["add-project", "--config", str(config_path), "--area", "infra", "--name", "widget"]
    assert main(argv) == 0
    (tmp_path / "dev" / "infra" / "widget").rmdir()
    recorded = registry_path.read_bytes()
    capsys.readouterr()

    status = main(argv)

    captured = capsys.readouterr()
    assert status != 0
    assert captured.err.strip() != ""
    assert captured.out == ""
    assert registry_path.read_bytes() == recorded
    assert not (tmp_path / "dev" / "infra" / "widget").exists()


def test_add_project_cli_reports_an_unreadable_configuration_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "add-project",
        "--config",
        str(tmp_path / "absent.toml"),
        "--area",
        "infra",
        "--name",
        "widget",
    ]

    status = main(argv)

    captured = capsys.readouterr()
    assert status == 1
    assert "cannot read configuration" in captured.err
    assert captured.out == ""


def _workspace_without_a_registry(tmp_path: Path) -> tuple[Path, Path]:
    """The fresh-host arrangement: a dev root, a config, and no registry file at all."""
    dev_root = tmp_path / "dev"
    (dev_root / "infra").mkdir(parents=True)
    registry_path = tmp_path / "never-created" / "projects-registry.yaml"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[paths]
dev_root = "{dev_root}"
registry_path = "{registry_path}"
database_path = "{tmp_path}/sessions.sqlite3"

[limits]
max_label_length = 40
project_page_size = 10
activity_poll_seconds = 30
activity_quiet_polls = 3
''',
        encoding="utf-8",
    )
    return config_path, registry_path


def test_add_project_creates_an_absent_registry_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """DEC-060: creating is allowed. Creating *silently* is the part that is not.

    Auto-creation turns one specific misconfiguration into a silent success: a `registry_path`
    that is typo'd, on an unmounted volume, or carrying a home baked in on another machine used
    to surface as `core: registry_unavailable` and get investigated. Without this notice it now
    produces a new empty registry at the wrong place, a zero exit, and a green `doctor`, while
    the operator's real registry sits untouched and unused.

    stdout stays exactly the created path, because things parse it. The notice is stderr's.
    """
    config_path, registry_path = _workspace_without_a_registry(tmp_path)
    created = tmp_path / "dev" / "infra" / "widget"
    argv = ["add-project", "--config", str(config_path), "--area", "infra", "--name", "widget"]

    assert main(argv) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == str(created), "stdout must stay the machine-readable path"
    assert str(registry_path) in captured.err, "the notice must name the file it created"
    assert "registry_path" in captured.err, "and point at the setting that would be wrong"
    assert load_registry(registry_path).projects, "the created registry must hold the project"


def test_add_project_stays_quiet_when_the_registry_already_existed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice is for the case that changed. An ordinary append must not grow noise."""
    config_path, _ = workspace(tmp_path)
    argv = ["add-project", "--config", str(config_path), "--area", "infra", "--name", "widget"]

    assert main(argv) == 0

    assert capsys.readouterr().err == ""
