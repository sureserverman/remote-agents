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
    assert set(artifact.path for artifact in supervisor.artifacts()) <= set(
        supervisor.installed_artifact_paths()
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


class _SupervisorWithHistory:
    """A supervisor that installed something under a name it no longer uses.

    The state DEC-051 is about, and one no shipped adapter is in yet: both currently retire
    nothing, so a sweep proved only against them proves the union is the installed set. This
    stands in for the version-after-next, which is the only version that can be hurt.
    """

    kind = SupervisorKind.SYSTEMD
    liveness_meaning = LivenessMeaning.RUNNING

    def __init__(self, directory: Path) -> None:
        self.home = directory
        self.current = directory / "remote-agents.service"
        self.abandoned = directory / "remote-agents-old.service"

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        return (SupervisorArtifact(path=self.current, content="a unit"),)

    def definition_path(self) -> Path:
        return self.current

    def installed_artifact_paths(self) -> tuple[Path, ...]:
        return (self.current,)

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        return (self.abandoned,)

    def required_directories(self) -> tuple[Path, ...]:
        return (self.current.parent,)

    def reload_command(self) -> tuple[str, ...]:
        return ()

    def install_command(self) -> tuple[str, ...]:
        return ("fake", "install")

    def remove_command(self) -> tuple[str, ...]:
        return ("fake", "remove")

    def start_command(self) -> tuple[str, ...]:
        return ("fake", "start")

    def liveness_command(self) -> tuple[str, ...]:
        return ("fake", "liveness")


def test_removal_sweeps_a_retired_name_no_current_version_would_install(tmp_path: Path) -> None:
    """DEC-051's whole point, and the case the shipped adapters cannot exercise.

    An artifact leaves the installed set by *moving* to the retired one rather than by
    disappearing. Dropping a name outright strands the file: removal sweeps what the installer
    knows it owns, so a definition no longer named is one no version of this tool can take away
    -- and the operator cannot work around it by uninstalling first, because that would mean
    running the *old* uninstaller before taking the upgrade.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)
    supervisor.current.write_text("a unit", encoding="utf-8")
    supervisor.abandoned.write_text("a unit an older version installed", encoding="utf-8")

    outcome = remove_daemon(supervisor, run=lambda argv: 0)

    assert not supervisor.current.exists()
    assert not supervisor.abandoned.exists(), "a retired name was left stranded"
    assert outcome.changed


def test_a_retired_name_is_swept_even_when_the_current_one_was_never_installed(
    tmp_path: Path,
) -> None:
    """The upgrade case: the old file is there and the new one never was.

    A sweep that reported "no daemon installed" because the *current* name is absent would leave
    the stranded file behind while telling the operator there was nothing to remove.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)
    supervisor.abandoned.write_text("a unit an older version installed", encoding="utf-8")

    outcome = remove_daemon(supervisor, run=lambda argv: 0)

    assert not supervisor.abandoned.exists()
    assert outcome.changed


@_PARAMS
def test_the_installed_and_retired_names_stay_disjoint(supervisor: ServiceSupervisor) -> None:
    """A path in both sets is a bookkeeping error, and `artifact_paths_to_remove` would hide it.

    That function dedupes, so an entry in both halves sweeps correctly and reads as fine -- while
    meaning the adapter believes it both does and does not install that name. The dedupe exists
    for a mid-migration adapter naming one file twice; this is what stops it becoming the way the
    ledger is kept.
    """
    installed = set(supervisor.installed_artifact_paths())
    retired = set(supervisor.retired_artifact_paths())

    assert not installed & retired


@_PARAMS
def test_the_ledger_covers_all_artifacts(supervisor: ServiceSupervisor) -> None:
    """Every path this version can write is a path this version can take away.

    The gate check named for this. It is the half of DEC-051 that is easy to keep true and easy
    to break silently: an adapter that grows a second artifact -- a drop-in, a wrapper, a
    timer -- and adds it to `artifacts()` alone leaves a file the uninstaller does not know
    exists, which is the same stranding as a dropped name arriving from the other direction.
    """
    written = {artifact.path for artifact in supervisor.artifacts()}
    swept = set(artifact_paths_to_remove(supervisor))

    assert written <= swept
    # `installed <= swept` is a tautology -- `artifact_paths_to_remove` *is* installed ∪ retired
    # -- so it was assertion-shaped and powerless. What is worth pinning is the other direction:
    # the installed half must cover everything rendered *and* the side-effect paths, which is
    # the widening a gate evaluator had to find by driving a real adapter.
    assert written < set(supervisor.installed_artifact_paths()) or not supervisor.artifacts()


def test_removal_leaves_a_foreign_file_in_the_same_directory_alone(tmp_path: Path) -> None:
    """`~/.config/systemd/user` and `~/Library/LaunchAgents` are shared directories.

    Other tools register their own units and agents there -- Homebrew services put LaunchAgents
    in exactly that folder -- so an uninstaller that swept a directory rather than a ledger would
    take somebody else's service down with ours. The sweep is over named paths for that reason,
    and this is what pins it: `install-agent-hooks` has the same rule about the operator's own
    hooks, and its docstring's line applies here unchanged -- this module has no way to give back
    what it deletes.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)
    supervisor.current.write_text("a unit", encoding="utf-8")
    someone_elses = tmp_path / "com.example.other.service"
    someone_elses.write_text("not ours", encoding="utf-8")
    look_alike = tmp_path / "remote-agents.service.bak"
    look_alike.write_text("an operator's own backup", encoding="utf-8")

    remove_daemon(supervisor, run=lambda argv: 0)

    assert not supervisor.current.exists()
    assert someone_elses.read_text(encoding="utf-8") == "not ours"
    assert look_alike.read_text(encoding="utf-8") == "an operator's own backup"


def test_removal_of_a_foreign_host_that_was_never_installed_to_is_a_reported_no_op(
    tmp_path: Path,
) -> None:
    """Uninstalling from a machine that was never installed to costs nothing and is not an error.

    Same rule `remove_agent_hooks` follows for a settings file that does not exist: an operator
    running the uninstaller to be sure is the ordinary case, and answering them with a failure
    teaches them to stop checking.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)

    outcome = remove_daemon(supervisor, run=lambda argv: 0)

    assert not outcome.changed
    assert "no daemon installed" in outcome.summary
    assert outcome.succeeded


def test_removal_is_safe_to_repeat_and_still_spares_a_foreign_file(tmp_path: Path) -> None:
    """The second run is the one an operator makes when they are unsure the first worked."""
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)
    supervisor.current.write_text("a unit", encoding="utf-8")
    someone_elses = tmp_path / "com.example.other.service"
    someone_elses.write_text("not ours", encoding="utf-8")

    first = remove_daemon(supervisor, run=lambda argv: 0)
    second = remove_daemon(supervisor, run=lambda argv: 0)

    assert first.changed and not second.changed
    assert "no daemon installed" in second.summary
    assert someone_elses.exists()


def test_a_removal_whose_unregister_failed_does_not_report_the_service_as_gone(
    tmp_path: Path,
) -> None:
    """Files deleted and the supervisor still holding the service is not a completed removal.

    `install_daemon` already refuses to call a failed register a success. The mirror case read as
    success: on a host with no session bus `disable` fails, the unit is deleted anyway, and the
    operator is told the daemon was removed while the supervisor still has it enabled.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)
    supervisor.current.write_text("a unit", encoding="utf-8")

    outcome = remove_daemon(supervisor, run=lambda argv: 1 if argv[-1] == "remove" else 0)

    assert not supervisor.current.exists()
    assert not outcome.succeeded
    assert "would not unregister" in outcome.summary


def test_a_removal_that_had_nothing_to_do_is_not_a_failure_when_unregister_refuses(
    tmp_path: Path,
) -> None:
    """`systemctl --user disable` on a unit that was never enabled exits non-zero.

    That is the ordinary answer on a host that was never installed to, so the no-op path must not
    read it as a failure -- which is why the status is consulted only where files were removed.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    supervisor = _SupervisorWithHistory(tmp_path)

    outcome = remove_daemon(supervisor, run=lambda argv: 1)

    assert not outcome.changed
    assert outcome.succeeded


@_PARAMS
def test_no_registered_adapter_would_sweep_a_foreign_directory(
    supervisor: ServiceSupervisor, tmp_path: Path
) -> None:
    """Every path either adapter can delete is a file it names, never a directory it lives in.

    A sweep that ever grew a `glob` or a `parent` would pass every test above -- they each plant
    one foreign file -- while taking out an arbitrary number of them on a real host. This asserts
    the shape rather than the sample: nothing in the ledger is a directory any other tool writes
    into, and every entry is a leaf.

    **The adapter is rebuilt under `tmp_path`, and that is not tidiness.** `_SUPERVISORS` holds
    the *registered* adapters, whose `home` defaults to `Path.home()`, and `remove_daemon`'s last
    act is `path.unlink()` on everything the sweep names. Driving it against those supervisors
    ran the real uninstaller against the developer's own machine: on 2026-08-26 this test deleted
    `~/.config/systemd/user/remote-agents.service` and the `enable` symlink beside it, leaving a
    service systemd went on running from a unit that no longer existed on disk -- and which no
    reboot could ever have brought back. It was silent because `run` is stubbed here, so no
    `systemctl disable` ever ran: the service was never stopped, and nothing reached the journal.

    The parametrisation is what makes this cover an adapter registered tomorrow, so it stays;
    only the home moves. `type(supervisor)` keeps each real adapter class under test.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon

    local = type(supervisor)(interpreter=supervisor.interpreter, home=tmp_path)
    swept = artifact_paths_to_remove(local)
    directories = set(local.required_directories())

    # Planted, so the drive below is load-bearing. With nothing on disk `remove_daemon` finds an
    # empty sweep and returns before its unlink loop, which is precisely the branch that has to
    # be exercised -- and the branch that did the damage. A foreign file in every required
    # directory is what a remover rewritten to scan those directories would take, so it fails
    # here as an assertion instead of on somebody's host.
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    foreign = tuple(directory / "another-tools-file.conf" for directory in directories)
    for path in foreign:
        path.write_text("not this tool's to delete", encoding="utf-8")
    for path in swept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("an artifact this tool installed", encoding="utf-8")

    # Driven through `remove_daemon` as well as read off the ledger, because reading the ledger
    # alone cannot catch a regression in the *remover*: a `remove_daemon` rewritten to scan
    # `required_directories()` itself would leave this assertion green. A reviewer was right that
    # the first version of this test claimed a role it did not have.
    recorded: list[tuple[str, ...]] = []
    remove_daemon(local, run=lambda argv: recorded.append(tuple(argv)) or 0)

    assert recorded, "removal ran no supervisor command at all"
    assert swept
    assert not directories & set(swept), "a directory is in the removal set"
    # Under the operator's own home, which is the boundary that actually matters, rather than
    # "inside a required directory" -- which was the first version of this assertion and was too
    # narrow twice over: it forbade the `default.target.wants` symlink `enable` creates, and it
    # forbade a *relocated* retired entry, which is half of the obligation this ledger carries.
    assert all(path.is_relative_to(local.home) for path in swept), (
        "a swept path escapes the operator's home"
    )
    assert not any(path.exists() for path in swept), "removal left one of its own artifacts"
    assert all(path.exists() for path in foreign), "removal deleted a file that was not its own"
    assert all(directory.is_dir() for directory in directories), "removal deleted a directory"


@pytest.mark.parametrize(
    "escape",
    ["/etc/hosts", "../outside/the/home", "sub/../../outside"],
    ids=["absolute", "parent-traversal", "traversal-through-a-subdirectory"],
)
def test_a_retired_entry_cannot_name_a_file_outside_the_operators_home(
    tmp_path: Path, escape: str
) -> None:
    """The adapters promise this and nothing enforced it, in code that deletes files.

    `Path(home) / "/etc/hosts"` is `/etc/hosts`, and `is_relative_to` is a string comparison that
    answers True for `<home>/../escapee`. A reviewer planted an absolute retired entry and watched
    `remove_daemon` delete a file in another tree. A guarantee stated in a docstring and backed by
    nothing is worse than none, because it is why nobody checks.
    """

    class _Escaping(_SupervisorWithHistory):
        def retired_artifact_paths(self) -> tuple[Path, ...]:
            return (Path(tmp_path) / escape,)

    with pytest.raises(ValueError):
        artifact_paths_to_remove(_Escaping(tmp_path))


@_PARAMS
def test_the_definition_path_is_one_of_the_paths_removal_sweeps(
    supervisor: ServiceSupervisor,
) -> None:
    """Whatever inspection names, removal must already know how to take away.

    `definition_path()` exists so an operator -- and the gate that greps the file -- can ask
    *where this host's daemon definition is* without asking what is in it. A path this project
    is willing to name and not willing to sweep would be DEC-051's stranding arriving through
    the one command whose whole job is to point at the file.
    """
    assert supervisor.definition_path() in set(supervisor.installed_artifact_paths())


@_PARAMS
def test_the_definition_path_is_where_the_definition_is_actually_rendered(
    supervisor: ServiceSupervisor,
) -> None:
    """The third answer to "which file", pinned to the other two wherever all three can be given.

    `artifacts()` says what is written, `installed_artifact_paths()` says what is left behind,
    and this says which of those is the definition. Three answers drift; this is what stops the
    named one pointing somewhere the renderer never writes.
    """
    (artifact,) = supervisor.artifacts()

    assert supervisor.definition_path() == artifact.path
