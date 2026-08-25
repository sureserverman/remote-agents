"""The supervisor vocabulary lives where neither supervisor can reach it.

`check_imports` proves the whole tree observes ARCH-02, which already covers this file the
moment it exists. This pins the one module by name anyway, because the port is the seam the
next two tasks build *against*: a systemd adapter and a launchd adapter are written in
parallel, each free to reach for something the other cannot honour, and the cheapest moment
to notice that the shared vocabulary grew an implementation import is before either of them
exists. A whole-tree check reports the same violation one commit later and one file further
from its cause.
"""

from dataclasses import dataclass
from pathlib import Path

from check_imports import internal_imports

from remote_agents.ports.service_supervisor import (
    LivenessMeaning,
    ServiceSupervisor,
    SupervisorArtifact,
    SupervisorKind,
    artifact_paths_to_remove,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
_PORT_PATH = _SOURCE_ROOT / "remote_agents" / "ports" / "service_supervisor.py"


def test_the_supervisor_port_is_a_port() -> None:
    """It has to be under `ports/` for the layer rule to bind it at all.

    A supervisor vocabulary parked at the package root, or inside one of the adapter
    families, is checked by nothing: `module_layer` would call it `root` or hand it that
    family's allowances, and every assertion below would still pass.
    """
    assert _PORT_PATH.is_file(), f"the supervisor port is not at {_PORT_PATH}"


def test_the_supervisor_port_imports_no_adapter() -> None:
    """DEC-001: systemd versus launchd is an adapter difference, never a port's business."""
    imported = [name for _, name in internal_imports(_PORT_PATH, _SOURCE_ROOT)]

    reached = [name for name in imported if name.startswith("remote_agents.adapters")]

    assert reached == [], f"the supervisor port reaches into an adapter: {reached}"


def test_the_port_carries_the_vocabulary_the_adapters_implement() -> None:
    """The names Tasks 2.2 and 2.3 are written against, so neither invents its own.

    Without this the previous two tests pass over an empty module: a file that imports
    nothing imports no adapter.
    """
    assert {kind.value for kind in SupervisorKind} == {"systemd", "launchd"}

    supervisor = _FakeSupervisor()

    assert isinstance(supervisor, ServiceSupervisor)
    assert artifact_paths_to_remove(supervisor) == (
        Path("/installed/now.conf"),
        Path("/installed/before.conf"),
    )


def test_the_removal_sweep_covers_what_older_versions_installed() -> None:
    """DEC-051 applied to daemon definitions: dropping a path must not strand it.

    Exercised rather than asserted about. `artifact_paths_to_remove` reads the installed
    paths off the rendered artifacts, and a version of it that reached for a member the
    protocol does not declare passed every `callable()`-shaped check written against it.
    """
    swept = artifact_paths_to_remove(_FakeSupervisor())

    assert Path("/installed/before.conf") in swept
    assert len(swept) == len(set(swept)), "removal would visit the same path twice"


@dataclass(frozen=True, slots=True)
class _FakeSupervisor:
    """A supervisor that is neither of the real two, which is the point.

    The port's own tests must not import an adapter -- that is the rule under test one
    function up -- so the thing that proves the vocabulary is implementable is implemented
    here, against the protocol alone.
    """

    kind: SupervisorKind = SupervisorKind.SYSTEMD
    liveness_meaning: LivenessMeaning = LivenessMeaning.RUNNING

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        return (SupervisorArtifact(path=Path("/installed/now.conf"), content="body"),)

    def installed_artifact_paths(self) -> tuple[Path, ...]:
        return tuple(artifact.path for artifact in self.artifacts())

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        return (Path("/installed/before.conf"), Path("/installed/now.conf"))

    def required_directories(self) -> tuple[Path, ...]:
        return ()

    def reload_command(self) -> tuple[str, ...]:
        return ()

    def install_command(self) -> tuple[str, ...]:
        return ("fake", "install")

    def remove_command(self) -> tuple[str, ...]:
        return ("fake", "remove")

    def start_command(self) -> tuple[str, ...]:
        return ("fake", "start")

    def liveness_command(self) -> tuple[str, ...]:
        return ("fake", "is-running")
