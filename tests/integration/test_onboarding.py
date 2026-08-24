"""Onboarding generates the operator's configuration; it never copies the shipped example.

`config/remote-agents.example.toml` hardcodes `/home/user/dev` and a `/home/user/.claude/…`
registry path. Copying it is what the README has told operators to do, and it cannot work on a
macOS host whose home is `/Users/…` -- `load_config` refuses `paths.dev_root` that is not an
existing directory, so the copy fails at the first `serve` rather than at install time, on the
platform this whole plan exists to support.

So the file is *rendered* from the home this process actually has, and the test that matters is
not that the renderer produced some text: it is that the text loads through the real loader,
with no drift, with nothing of this developer's own home in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.bootstrap import detected_config
from remote_agents.config import ConfigError, describe_schema_drift, load_config, render_config
from remote_agents.production import ProductionPaths


@pytest.fixture
def a_foreign_home(tmp_path: Path) -> Path:
    """A home that is not this host's and is not under `/home/user`, with a dev tree in it.

    `Users` rather than `home`, because the failure this task exists to prevent is specific to a
    macOS home, and a fixture spelled `home` would have reproduced the shape that already works.
    """
    home = tmp_path / "Users" / "tester"
    (home / "dev").mkdir(parents=True)
    return home


def test_config_generated_for_a_foreign_home_loads_through_the_real_loader(
    a_foreign_home: Path, tmp_path: Path
) -> None:
    """The whole task in one assertion: what onboarding writes is what `serve` can read."""
    paths = ProductionPaths.for_home(a_foreign_home)
    paths.config_directory.mkdir(parents=True)
    paths.config_path.write_text(detected_config(a_foreign_home), encoding="utf-8")

    config = load_config(paths.config_path)

    assert config.dev_root == a_foreign_home / "dev"
    assert config.registry_path == a_foreign_home / ".claude" / "projects-registry.yaml"
    assert config.database_path == paths.database_path


def test_config_generated_for_a_foreign_home_reports_no_schema_drift(
    a_foreign_home: Path,
) -> None:
    """`doctor`'s own comparison, run against the file onboarding just wrote.

    A file that loads can still differ from the schema this build requires -- that is what
    `describe_schema_drift` exists to report and what `doctor` shows an operator. A generated
    config that arrives already drifted would put a complaint on the first report the operator
    ever reads.
    """
    paths = ProductionPaths.for_home(a_foreign_home)
    paths.config_directory.mkdir(parents=True)
    paths.config_path.write_text(detected_config(a_foreign_home), encoding="utf-8")

    drift = describe_schema_drift(paths.config_path)

    assert drift["readable"] is True
    assert drift["unknown"] == []
    assert drift["missing"] == []
    assert drift["invalid"] == []


def test_config_carries_nothing_of_the_developers_own_home(a_foreign_home: Path) -> None:
    """The example's two literals are the ones that cannot survive a different host."""
    generated = detected_config(a_foreign_home)

    assert "/home/user" not in generated
    assert str(a_foreign_home) in generated


def test_config_renders_every_key_the_loader_requires_and_no_other() -> None:
    """Derived from the schema, not restated beside it.

    A key added to `load_config`'s closed sets and forgotten here would produce a file that
    fails `_require_exact_keys` on the operator's host and nowhere else. The renderer refuses to
    write a file it knows is incomplete, so the failure lands on whoever adds the key.
    """
    with pytest.raises(ConfigError):
        render_config(
            dev_root=Path("/Users/tester/dev"),
            registry_path=Path("/Users/tester/.claude/projects-registry.yaml"),
            database_path=Path("/Users/tester/.local/state/remote-agents/sessions.sqlite3"),
            limits={"max_label_length": 40},
        )


def test_config_quotes_a_home_that_would_otherwise_break_the_toml(tmp_path: Path) -> None:
    """A home holding a quote or a backslash is a home, and TOML has an escape for both.

    The systemd adapter learned this about unit files the expensive way -- an apostrophe in a
    home directory left the unit with no `ExecStart` at all. The same character reaches this
    renderer, and TOML's basic-string escapes are what stop it ending the string early.
    """
    home = tmp_path / 'q"uote\\slash'
    (home / "dev").mkdir(parents=True)

    config = load_config_from_text(tmp_path, detected_config(home))

    assert config.dev_root == home / "dev"


def load_config_from_text(directory: Path, text: str):
    """Write a generated config where a loader can read it, and load it."""
    path = directory / "generated.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)
