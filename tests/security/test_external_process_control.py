"""External termination cannot turn into arbitrary local process control."""

from pathlib import Path

from remote_agents.adapters.processes.linux_control import LinuxExternalProcessController
from remote_agents.domain.external_sessions import ExternalProcessIdentity, ExternalStopOutcome


async def test_controller_refuses_a_foreign_uid_before_any_signal() -> None:
    controller = LinuxExternalProcessController(effective_uid=1000)
    result = await controller.terminate(ExternalProcessIdentity(42, 9, 1001, "claude"))

    assert result.outcome is ExternalStopOutcome.IDENTITY_CHANGED


def test_controller_contains_only_the_fixed_signal_and_no_group_or_shell_path() -> None:
    contents = Path("src/remote_agents/adapters/processes/linux_control.py").read_text(
        encoding="utf-8"
    )

    assert "signal.SIGTERM" in contents
    assert "SIGKILL" not in contents
    assert "killpg" not in contents
    assert "os.kill(" not in contents
    assert "shell=True" not in contents
