"""Tests for the import-boundary checker itself."""

from pathlib import Path

from check_imports import find_violations


def write_module(source_root: Path, relative_path: str, content: str) -> None:
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_checker_rejects_a_domain_import_of_an_adapter(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/domain/models.py",
        "import remote_agents.adapters.tmux.gateway\n",
    )

    violations = find_violations(source_root)

    assert len(violations) == 1
    assert violations[0].reason == "forbidden from domain"


def test_checker_accepts_domain_internal_imports(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/domain/state.py",
        "import remote_agents.domain.models\n",
    )

    assert find_violations(source_root) == []


def test_checker_rejects_imports_between_adapter_families(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/adapters/tmux/gateway.py",
        "import remote_agents.adapters.sqlite.database\n",
    )

    assert len(find_violations(source_root)) == 1


def test_checker_allows_the_driving_telegram_adapter_to_invoke_application(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/adapters/telegram/service.py",
        "import remote_agents.application.commands\nimport remote_agents.config\n",
    )

    assert find_violations(source_root) == []


def test_checker_rejects_telegram_to_tmux_coupling(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/adapters/telegram/service.py",
        "import remote_agents.adapters.tmux.gateway\n",
    )

    violations = find_violations(source_root)

    assert len(violations) == 1
    assert violations[0].reason == "forbidden from adapters"


def test_checker_rejects_non_bootstrap_composition_import(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/config.py",
        "import remote_agents.adapters.sqlite.session_store\n",
    )

    violations = find_violations(source_root)

    assert len(violations) == 1
    assert violations[0].reason == "forbidden from root"


def test_checker_resolves_and_rejects_relative_domain_to_adapter_import(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/domain/models.py",
        "from ..adapters.tmux import gateway\n",
    )

    assert len(find_violations(source_root)) == 1
