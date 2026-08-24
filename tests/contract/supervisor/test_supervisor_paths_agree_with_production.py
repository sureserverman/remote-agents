"""The generated definitions point at the paths the rest of the project actually uses.

`ProductionPaths` owns `~/.config/remote-agents/config.toml` and `~/.config/systemd/user/`.
Both supervisor adapters reconstruct those segments by hand, and **cannot** do otherwise:
ARCH-02 lets an adapter import `domain`, `ports` and its own family, while `production.py` is
a root-layer module that may itself import only `config` and `production`. So neither end can
legally hold a shared constant, and the duplication is a consequence of the boundary rather
than an oversight.

What the boundary does not excuse is the *silence*. Three hand-written copies of one path drift
without anything noticing -- an XDG_CONFIG_HOME option is the obvious future change -- and the
failure is quiet in the worst way: the service would start, read a config file nobody else
writes, and behave as though it had no configuration at all.

A test is where the parity can live, because `check_imports.py` walks `src/` only. This file is
that safety net, and it is the whole reason the duplication is acceptable as shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.supervisor.launchd import LaunchdSupervisor
from remote_agents.adapters.supervisor.systemd import UNIT_NAME, SystemdSupervisor
from remote_agents.production import ProductionPaths

_HOME = Path("/home/tester")
_INTERPRETER = Path("/opt/ra/bin/python3")


def _config_argument(argv_line: str) -> str:
    """The value `--config` was given, as it appears in a rendered definition."""
    _, _, tail = argv_line.partition("--config ")
    return tail.strip().strip('"')


def test_the_systemd_unit_is_written_where_production_expects_units() -> None:
    """`unit_path` and `ProductionPaths.unit_directory` must not drift apart."""
    (artifact,) = SystemdSupervisor(interpreter=_INTERPRETER, home=_HOME).artifacts()

    assert artifact.path == ProductionPaths.for_home(_HOME).unit_directory / UNIT_NAME


def test_the_systemd_unit_starts_the_service_on_productions_config() -> None:
    """A unit pointing at a config file nothing else writes starts an unconfigured service."""
    (artifact,) = SystemdSupervisor(interpreter=_INTERPRETER, home=_HOME).artifacts()
    execstart = next(
        line for line in artifact.content.splitlines() if line.startswith("ExecStart=")
    )

    assert _config_argument(execstart) == str(ProductionPaths.for_home(_HOME).config_path)


def test_the_launchd_plist_starts_the_service_on_productions_config() -> None:
    """The same parity on the other adapter, which renders the same path independently."""
    launchd = LaunchdSupervisor(
        interpreter=_INTERPRETER,
        home=_HOME,
        uid=501,
        homebrew_prefix=lambda: None,
    )
    (artifact,) = launchd.artifacts()

    import plistlib

    arguments = plistlib.loads(artifact.content.encode("utf-8"))["ProgramArguments"]

    assert arguments[arguments.index("--config") + 1] == str(
        ProductionPaths.for_home(_HOME).config_path
    )


@pytest.mark.parametrize(
    "supervisor",
    [
        SystemdSupervisor(interpreter=_INTERPRETER, home=_HOME),
        LaunchdSupervisor(
            interpreter=_INTERPRETER, home=_HOME, uid=501, homebrew_prefix=lambda: None
        ),
    ],
    ids=["systemd", "launchd"],
)
def test_both_adapters_agree_with_each_other_on_the_config_path(supervisor) -> None:
    """Stated as a property of every adapter, so a third one is covered on the day it lands."""
    (artifact,) = supervisor.artifacts()

    assert str(ProductionPaths.for_home(_HOME).config_path) in artifact.content
