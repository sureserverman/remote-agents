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
activity_poll_seconds = 30
activity_quiet_polls = 3
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


def test_activity_polling_limits_are_loaded_and_bounded(tmp_path: Path) -> None:
    """Both knobs the pane watcher runs on, validated like every other limit."""
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.activity_poll_seconds == 30
    assert config.activity_quiet_polls == 3


@pytest.mark.parametrize(
    "replacement",
    (
        "activity_poll_seconds = 4",
        "activity_poll_seconds = 601",
        "activity_quiet_polls = 1",
        "activity_quiet_polls = 21",
    ),
)
def test_an_activity_limit_outside_its_bounds_is_refused(tmp_path: Path, replacement: str) -> None:
    """A poll every second is self-inflicted load; one quiet poll is not evidence of quiet.

    The lower bound on `activity_quiet_polls` is the one that carries meaning: at 1, "quiet"
    means a single capture matched the one before it, which any agent between two lines of
    output satisfies. The claim is that output *stopped*, and one poll cannot support it.
    """
    key = replacement.split(" =")[0]
    invalid = (
        example(tmp_path).replace(f"{key} = 30", replacement).replace(f"{key} = 3", replacement)
    )

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, invalid))

    assert key in str(refusal.value)


def test_an_absent_activity_limit_is_refused_by_the_exact_key_schema(tmp_path: Path) -> None:
    """The schema is exact, so a config written before these knobs existed fails loudly.

    Defaulting a missing limit would leave the operator's file silently disagreeing with the
    service it configures, which is what an exact-key schema exists to prevent.
    """
    without = example(tmp_path).replace("activity_poll_seconds = 30\n", "")

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, without))

    assert "activity_poll_seconds" in str(refusal.value)


def test_the_shipped_example_config_carries_both_activity_limits() -> None:
    """The README installs from this file, so a knob absent here is a broken first run."""
    shipped = Path("config/remote-agents.example.toml").read_text(encoding="utf-8")

    assert "activity_poll_seconds" in shipped
    assert "activity_quiet_polls" in shipped
