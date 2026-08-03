"""Linux process discovery exposes only safe, provider-correlated summaries."""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.processes.linux import LinuxLocalProcessCatalog
from remote_agents.domain.external_sessions import ExternalSessionState, ExternalStopEligibility
from remote_agents.domain.models import ProjectId


async def test_linux_discovery_marks_only_a_tty_bound_claude_artifact_as_running_externally(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    session_root = tmp_path / "claude" / "projects"
    transcript = (
        session_root
        / str(project).replace("/", "-")
        / "01234567-89ab-cdef-0123-456789abcdef.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("private transcript", encoding="utf-8")
    _process(
        proc_root, 41, executable="claude", cwd=project, terminal="/dev/pts/9", artifact=transcript
    )
    _process(proc_root, 42, executable="codex", cwd=project, terminal="/dev/pts/10")

    catalogue = LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root, claude_sessions_root=session_root
    )
    sessions = await catalogue.list_external_sessions()

    assert [session.state for session in sessions] == [
        ExternalSessionState.RUNNING_EXTERNALLY,
        ExternalSessionState.NOT_SAFELY_ADOPTABLE,
    ]
    resolved = await catalogue.resolve_external_session(sessions[0].reference)
    assert resolved is not None
    assert resolved.provider_conversation_id is not None
    assert resolved.provider_conversation_id.value == "01234567-89ab-cdef-0123-456789abcdef"


async def test_linux_discovery_excludes_a_descendant_of_a_managed_pane_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _process(proc_root, 100, executable="bash", cwd=project, terminal="/dev/pts/1")
    _process(proc_root, 101, executable="claude", cwd=project, terminal="/dev/pts/2", ppid=100)

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions(excluded_process_roots=(100,))

    assert sessions == ()


async def test_linux_discovery_marks_an_uncorrelated_curated_process_selection_required(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _process(proc_root, 41, executable="codex", cwd=project, terminal="/dev/pts/10")

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions()

    assert sessions[0].stop_eligibility is ExternalStopEligibility.SELECTION_REQUIRED


async def test_linux_discovery_fails_closed_when_provider_artifact_or_tty_is_missing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _process(proc_root, 41, executable="claude", cwd=project, terminal="pipe:[1]")

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project},
        proc_root=proc_root,
        claude_sessions_root=tmp_path / "claude",
    ).list_external_sessions()

    assert sessions[0].state is ExternalSessionState.NOT_SAFELY_ADOPTABLE


async def test_linux_discovery_still_lists_a_provider_when_its_terminal_link_is_protected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _process(proc_root, 41, executable="claude", cwd=project, terminal="/dev/pts/9")
    (proc_root / "41" / "fd" / "0").unlink()

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions()

    assert len(sessions) == 1
    assert sessions[0].state is ExternalSessionState.NOT_SAFELY_ADOPTABLE


async def test_linux_discovery_falls_back_to_a_bounded_process_name_when_metadata_links_are_hidden(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _process(proc_root, 41, executable="claude", cwd=project, terminal="/dev/pts/9")
    (proc_root / "41" / "exe").unlink()
    (proc_root / "41" / "cwd").unlink()
    (proc_root / "41" / "fd" / "0").unlink()
    (proc_root / "41" / "comm").write_text("claude\n", encoding="utf-8")

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions()

    assert len(sessions) == 1
    assert str(sessions[0].profile_id) == "claude"
    assert sessions[0].project_id is None
    assert sessions[0].state is ExternalSessionState.NOT_SAFELY_ADOPTABLE


async def test_linux_discovery_recognizes_the_installed_claude_version_binary(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _process(
        proc_root,
        41,
        executable=".local/share/claude/versions/2.1.220",
        cwd=project,
        terminal="/dev/pts/9",
    )

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions()

    assert len(sessions) == 1
    assert str(sessions[0].profile_id) == "claude"


async def test_linux_discovery_prioritizes_recent_processes_within_its_bound(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    for pid in range(1, 1_025):
        (proc_root / str(pid)).mkdir(parents=True)
    _process(proc_root, 2_000, executable="codex", cwd=project, terminal="/dev/pts/10")

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions()

    assert len(sessions) == 1
    assert str(sessions[0].profile_id) == "codex"


def _process(
    proc_root: Path,
    pid: int,
    *,
    executable: str,
    cwd: Path,
    terminal: str,
    artifact: Path | None = None,
    ppid: int = 1,
) -> None:
    directory = proc_root / str(pid)
    (directory / "fd").mkdir(parents=True)
    executable_path = proc_root.parent / executable
    executable_path.parent.mkdir(parents=True, exist_ok=True)
    executable_path.touch()
    (directory / "exe").symlink_to(executable_path)
    (directory / "cwd").symlink_to(cwd)
    (directory / "fd" / "0").symlink_to(terminal)
    (directory / "stat").write_text(
        f"{pid} ({executable}) S {ppid} " + "0 " * 17 + "9\n", encoding="utf-8"
    )
    if artifact is not None:
        (directory / "fd" / "4").symlink_to(artifact)
