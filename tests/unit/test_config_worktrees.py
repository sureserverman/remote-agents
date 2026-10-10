"""The optional `[worktrees]` table: the copy list, its per-project override, and what it may
never name. And the folder session worktrees live in."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from remote_agents.config import (
    DEFAULT_WORKTREE_COPY,
    ConfigError,
    describe_schema_drift,
    load_config,
)
from remote_agents.production import ProductionPaths


def _config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'''[paths]
dev_root = "{tmp_path}"
registry_path = "{tmp_path}/registry.yaml"
database_path = "{tmp_path}/sessions.sqlite3"

[limits]
max_label_length = 40
project_page_size = 10
activity_poll_seconds = 30
'''
        + extra,
        encoding="utf-8",
    )
    return path


def test_a_file_without_the_table_loads_the_four_defaults(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    assert DEFAULT_WORKTREE_COPY == (
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/settings.local.json",
        ".claude/vault-context.md",
    )
    assert config.worktree_copy_for(tmp_path / "any") == DEFAULT_WORKTREE_COPY
    drift = describe_schema_drift(_config(tmp_path))
    assert not drift.get("unknown") and not drift.get("missing"), drift


def test_a_per_project_list_replaces_the_default_for_that_project_only(tmp_path: Path) -> None:
    config = load_config(
        _config(
            tmp_path,
            '\n[worktrees]\ncopy = ["CLAUDE.md"]\n\n[worktrees.projects]\n'
            f'"{tmp_path}/special" = ["AGENTS.md", "notes/setup.md"]\n',
        )
    )
    assert config.worktree_copy_for(tmp_path / "special") == ("AGENTS.md", "notes/setup.md")
    assert config.worktree_copy_for(tmp_path / "other") == ("CLAUDE.md",)
    drift = describe_schema_drift(_config(tmp_path, '\n[worktrees]\ncopy = ["CLAUDE.md"]\n'))
    assert not drift.get("unknown"), drift


@pytest.mark.parametrize(
    "entry",
    [
        ".env",
        ".env.local",
        "config/.env",
        ".venv",
        ".venv/bin/python",
        "../x",
        "a/../../x",
        "/etc/passwd",
        ".git/config",
        "sub/.git/hooks/pre-commit",
        "",
        ".",
        "./",
        ".ENV",
        ".Env.local",
        ".GIT/config",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "certs/site.pem",
        " CLAUDE.md",
        "CLAUDE.md\t",
        "a\\u0007b",  # a TOML escape: the loader decodes it to BEL
    ],
)
def test_a_forbidden_entry_is_refused_with_its_key(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ConfigError, match=r"worktrees\.copy"):
        load_config(_config(tmp_path, f'\n[worktrees]\ncopy = ["CLAUDE.md", "{entry}"]\n'))


def test_a_forbidden_per_project_entry_names_its_project_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"worktrees\.projects"):
        load_config(_config(tmp_path, f'\n[worktrees.projects]\n"{tmp_path}/p" = [".env"]\n'))


def test_a_relative_project_key_and_an_unknown_key_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"worktrees\.projects"):
        load_config(_config(tmp_path, '\n[worktrees.projects]\n"relative/p" = ["CLAUDE.md"]\n'))
    with pytest.raises(ConfigError, match="worktrees"):
        load_config(_config(tmp_path, '\n[worktrees]\nmove = ["CLAUDE.md"]\n'))


def test_a_project_path_naming_a_secret_word_is_not_mistaken_for_a_secret(
    tmp_path: Path,
) -> None:
    config = load_config(
        _config(tmp_path, f'\n[worktrees.projects]\n"{tmp_path}/token-bucket" = ["AGENTS.md"]\n')
    )
    assert config.worktree_copy_for(tmp_path / "token-bucket") == ("AGENTS.md",)


def test_the_worktree_directory_is_private_and_under_state(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    assert paths.worktree_directory.parent == paths.state_directory
    paths.ensure_directories(include_unit_directory=False)
    assert paths.worktree_directory.is_dir()
    assert stat.S_IMODE(paths.worktree_directory.stat().st_mode) == 0o700
