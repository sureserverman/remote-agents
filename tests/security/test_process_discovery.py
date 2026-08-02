"""External discovery stays read-only and does not inspect process content sources."""

from pathlib import Path

from remote_agents.adapters.processes.linux import LinuxLocalProcessCatalog
from remote_agents.domain.models import ProjectId


async def test_discovery_does_not_open_sensitive_process_content_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    directory = proc_root / "7"
    (directory / "fd").mkdir(parents=True)
    executable = tmp_path / "claude"
    executable.touch()
    (directory / "exe").symlink_to(executable)
    (directory / "cwd").symlink_to(project)
    (directory / "fd" / "0").symlink_to("/dev/pts/7")
    (directory / "cmdline").write_text("must not be read", encoding="utf-8")
    (directory / "environ").write_text("must not be read", encoding="utf-8")

    sessions = await LinuxLocalProcessCatalog(
        {ProjectId("opaque-editor"): project}, proc_root=proc_root
    ).list_external_sessions()

    assert sessions[0].project_id == ProjectId("opaque-editor")
