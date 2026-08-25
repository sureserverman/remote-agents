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


@_PARAMS
def test_every_required_directory_is_absolute_and_contains_an_installed_artifact_or_log(
    supervisor: ServiceSupervisor,
) -> None:
    """An installer creates these before installing; a wrong answer is a cold-start failure.

    Stated as a property of every adapter because both supervisors needed it and neither could
    say so: launchd opens a job's log files itself, before the process runs, and `install(1)`
    creates no parent for a systemd unit. The directory holding each rendered artifact must be
    named, or the installer has no way to know it owes it.
    """
    required = supervisor.required_directories()

    assert required, f"{supervisor.kind.value} names no directory an installer must create"
    assert all(path.is_absolute() for path in required), required
    for artifact in supervisor.artifacts():
        assert artifact.path.parent in required, (
            f"{artifact.path} is installed into a directory no one is told to create"
        )


@_PARAMS
def test_removal_can_never_sweep_the_operators_own_files(supervisor: ServiceSupervisor) -> None:
    """The uninstaller takes away daemon definitions and cannot reach a config or a credential.

    `remove_daemon` deletes every path `artifact_paths_to_remove` returns, so what that union may
    contain is the whole of what an uninstall can destroy. Until this test, the guarantee that it
    never contains the operator's own files rested entirely on docstring discipline -- and a
    credential file deleted by an uninstaller is the one loss in this project that cannot be
    undone, because its contents came from Telegram and not from anything on the host.

    Checked against `ProductionPaths` rather than against a literal, so a future change to where
    either file lives is checked too rather than silently escaping the assertion.
    """
    from remote_agents.production import ProductionPaths

    paths = ProductionPaths.for_home(Path("/home/tester"))
    operator_files = {paths.config_path, paths.environment_path, paths.database_path}

    assert not operator_files & set(artifact_paths_to_remove(supervisor))


@_PARAMS
def test_where_the_artifacts_go_agrees_with_where_they_are_rendered(
    supervisor: ServiceSupervisor,
) -> None:
    """Two answers to one question, pinned together wherever both can be given.

    `installed_artifact_paths()` exists so removal never has to render -- the systemd adapter
    refuses at render time to describe an executable systemd would not start, and reaching that
    refusal through `artifacts()` made the one host this tool declines to install to the one it
    could never uninstall from. Splitting the question is what fixed it; this is what stops the
    two halves drifting into different answers.
    """
    assert supervisor.installed_artifact_paths() == tuple(
        artifact.path for artifact in supervisor.artifacts()
    )


@_PARAMS
def test_the_reload_verb_is_argv_or_deliberately_nothing(supervisor: ServiceSupervisor) -> None:
    """The eighth verb is the one `_VERBS` cannot check, because `()` is a legal answer.

    launchd has no cached fragment and so nothing to reload, and saying so with an empty tuple is
    right. That exemption is why this needs its own assertion: without one, the only verb allowed
    to be empty is also the only verb whose shape nothing checks.
    """
    argv = supervisor.reload_command()

    assert isinstance(argv, tuple)
    assert all(isinstance(word, str) and word for word in argv)
