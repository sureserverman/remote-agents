"""The config-backed limits-source setting: the one writer a Settings row reaches.

Integration rather than unit, because the thing under test is the seam between a port-shaped
capability and `config`'s real reader and writer -- and `write_limits_key`'s refusals are the
half that matters. It refuses several shapes an owner can produce by hand, and the port's
contract is that none of them reaches a screen as an exception; the row finds out by reading
back and seeing its own intention missing.

It lives in `composition/` rather than in an adapter for a reason `check_imports.py` enforces:
`adapters/agents/claude` is not a driver adapter and may not import `remote_agents.config` at
all. Sub-plan 01 hit the same wall on the read side and answered it the same way -- the root
hands the adapter a `partial(read_claude_limits_source, path)` rather than letting the package
open the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.composition.limits_source import ConfigLimitsSource
from remote_agents.config import DEFAULT_CLAUDE_LIMITS_SOURCE, read_claude_limits_source

_CONFIG = """
[paths]
dev_root = "/tmp/dev"

[limits]
claude_context_window = 200000
"""


def _config(tmp_path: Path, body: str = _CONFIG) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


async def test_a_default_config_reads_as_the_hop(tmp_path: Path) -> None:
    setting = ConfigLimitsSource(_config(tmp_path))

    assert await setting.read() == DEFAULT_CLAUDE_LIMITS_SOURCE


async def test_a_written_source_reads_back_through_the_config(tmp_path: Path) -> None:
    """Through `config`'s own reader, not the object's memory -- a setting that cached its
    last write would report success for a write the file refused."""
    path = _config(tmp_path)
    setting = ConfigLimitsSource(path)

    await setting.write("usage-api")

    assert await setting.read() == "usage-api"
    assert read_claude_limits_source(path) == "usage-api"


async def test_writing_keeps_every_other_byte_of_the_owner_s_file(tmp_path: Path) -> None:
    """DEC-088's whole argument: this is a hand-written document with one line changed, never
    a re-render of the schema."""
    body = _CONFIG + '\n# a comment the owner wrote\nclaude_limits_source = "status-line"\n'
    path = _config(tmp_path, body)
    setting = ConfigLimitsSource(path)

    await setting.write("usage-api")

    written = path.read_text(encoding="utf-8")
    assert "# a comment the owner wrote" in written
    assert "claude_context_window = 200000" in written
    assert 'dev_root = "/tmp/dev"' in written


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("this is not toml at all [[[", "a file that does not parse"),
        ('[paths]\ndev_root = "/tmp/dev"\n', "a file with no [limits] table"),
    ],
)
async def test_a_refused_write_is_not_an_exception_and_the_read_back_reports_it(
    tmp_path: Path, body: str, why: str
) -> None:
    """The port's contract, and the one the Settings row depends on: `write` never raises, so
    the row detects a refusal by the read-back not being what it asked for."""
    path = _config(tmp_path, body)
    setting = ConfigLimitsSource(path)

    await setting.write("usage-api")

    assert await setting.read() != "usage-api", why


async def test_a_missing_config_file_reads_as_the_default_and_refuses_the_write(
    tmp_path: Path,
) -> None:
    """A host that never ran `serve` has no file. Reading is the default; writing declines
    rather than minting a config this project did not render."""
    setting = ConfigLimitsSource(tmp_path / "absent.toml")

    assert await setting.read() == DEFAULT_CLAUDE_LIMITS_SOURCE
    await setting.write("usage-api")
    assert await setting.read() == DEFAULT_CLAUDE_LIMITS_SOURCE


async def test_a_value_outside_the_closed_set_is_refused_rather_than_stored(
    tmp_path: Path,
) -> None:
    """The reader forgives an unknown source; the writer must not be what creates one."""
    path = _config(tmp_path)
    setting = ConfigLimitsSource(path)

    await setting.write("carrier-pigeon")

    assert await setting.read() == DEFAULT_CLAUDE_LIMITS_SOURCE
