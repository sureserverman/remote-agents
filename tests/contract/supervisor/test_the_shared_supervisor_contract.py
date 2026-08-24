"""Every registered supervisor satisfies the same contract, by construction rather than by copy.

The per-adapter files each assert the shared obligations separately, which satisfies the gate
in effect but not structurally: a third adapter would have to *remember* to copy four tests,
and nothing would fail if it did not. Parametrizing over `registered_supervisors()` makes the
contract binding on whatever is in the registry -- the same shape the credential sweep already
uses, and for the same reason.

The per-adapter assertions stay where they are. They pin values this file cannot know (which
argv, which path), and this file pins the obligations that hold regardless of those values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.supervisor import SUPERVISOR_FACTORIES, registered_supervisors
from remote_agents.ports.service_supervisor import (
    LivenessMeaning,
    ServiceSupervisor,
    SupervisorArtifact,
    SupervisorKind,
    artifact_paths_to_remove,
)

_VERBS = ("install_command", "remove_command", "start_command", "liveness_command")


def _registered() -> list[ServiceSupervisor]:
    return list(registered_supervisors())


def _ids(supervisors: list[ServiceSupervisor]) -> list[str]:
    return [supervisor.kind.value for supervisor in supervisors]


_SUPERVISORS = _registered()
_PARAMS = pytest.mark.parametrize("supervisor", _SUPERVISORS, ids=_ids(_SUPERVISORS))


def test_every_supervisor_kind_has_exactly_one_registered_adapter() -> None:
    """The registry covers the enum, so parametrizing over it is not a partial sweep.

    Asserted first, and for the reason the credential sweep asserts the same thing: every test
    below is parametrized over the registry, so an empty or partial registry would turn this
    whole file green while checking nothing.
    """
    assert set(SUPERVISOR_FACTORIES) == set(SupervisorKind)
    assert {supervisor.kind for supervisor in _registered()} == set(SupervisorKind)


@_PARAMS
def test_the_adapter_satisfies_the_port(supervisor: ServiceSupervisor) -> None:
    assert isinstance(supervisor, ServiceSupervisor)
    assert isinstance(supervisor.kind, SupervisorKind)
    assert isinstance(supervisor.liveness_meaning, LivenessMeaning)


@_PARAMS
def test_every_verb_is_a_non_empty_tuple_of_strings(supervisor: ServiceSupervisor) -> None:
    """Argv the caller runs -- so a bare string, which would be run character by character
    by some callers and as a shell line by others, is exactly the shape to refuse."""
    for verb in _VERBS:
        argv = getattr(supervisor, verb)()

        assert isinstance(argv, tuple), f"{supervisor.kind.value}.{verb} is not a tuple"
        assert argv, f"{supervisor.kind.value}.{verb} is empty"
        assert all(isinstance(word, str) for word in argv), argv


@_PARAMS
def test_every_rendered_artifact_is_absolute_and_non_empty(supervisor: ServiceSupervisor) -> None:
    """The rule that holds on both supervisors, since neither format can defer a home."""
    artifacts = supervisor.artifacts()

    assert artifacts, f"{supervisor.kind.value} renders nothing"
    for artifact in artifacts:
        assert isinstance(artifact, SupervisorArtifact)
        assert artifact.path.is_absolute(), artifact.path
        assert artifact.content.strip(), f"{artifact.path} is empty"


@_PARAMS
def test_removal_sweeps_the_installed_set_and_never_repeats_a_path(
    supervisor: ServiceSupervisor,
) -> None:
    """DEC-051's union, stated as an obligation on every adapter rather than on two of them."""
    swept = artifact_paths_to_remove(supervisor)

    for artifact in supervisor.artifacts():
        assert artifact.path in swept, f"{artifact.path} is installed but never removed"
    for retired in supervisor.retired_artifact_paths():
        assert retired in swept, f"{retired} was installed once and is now stranded"
    assert len(swept) == len(set(swept)), "removal would visit the same path twice"
    assert all(path.is_absolute() for path in swept), swept


@_PARAMS
def test_the_adapter_refuses_a_relative_home(supervisor: ServiceSupervisor) -> None:
    """Every adapter enforces the absolute-path invariant its own docstrings assert."""
    with pytest.raises(ValueError):
        type(supervisor)(interpreter=Path("/opt/ra/bin/python3"), home=Path("relative/home"))
