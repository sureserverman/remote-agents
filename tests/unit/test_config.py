"""Closed-schema and secret-separation tests."""

from pathlib import Path

import pytest

from remote_agents.config import ConfigError, load_config, load_secrets


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def example(tmp_path: Path) -> str:
    return f'''[paths]
dev_root = "{tmp_path}"
registry_path = "{tmp_path}/registry.yaml"
database_path = "{tmp_path}/sessions.sqlite3"

[limits]
max_label_length = 40
project_page_size = 10
'''


def test_load_config_accepts_closed_example(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.dev_root == tmp_path.resolve()


@pytest.mark.parametrize("replacement", ["max_label_length = 41", "project_page_size = 0"])
def test_load_config_rejects_limits_outside_bounds(tmp_path: Path, replacement: str) -> None:
    invalid = example(tmp_path).replace("max_label_length = 40", replacement)

    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, invalid))


@pytest.mark.parametrize("addition", ['token = "secret"', "unknown = true"])
def test_load_config_rejects_secret_or_unknown_keys(tmp_path: Path, addition: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, example(tmp_path) + addition + "\n"))


def test_load_config_rejects_a_missing_dev_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, example(tmp_path).replace(str(tmp_path), "/missing", 1)))


def test_production_secrets_require_all_environment_values() -> None:
    with pytest.raises(ConfigError):
        load_secrets({}, production=True)
