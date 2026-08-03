"""Opt-in disposable proof of the fixed external-controller boundary."""

from __future__ import annotations

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest

from remote_agents.adapters.processes.linux_control import LinuxExternalProcessController
from remote_agents.domain.external_sessions import ExternalStopOutcome


@pytest.mark.live_acceptance
async def test_external_controller_terminates_only_a_transient_restricted_helper() -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_EXTERNAL_CONTROL") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_EXTERNAL_CONTROL is not enabled")
    unit = f"remote-agents-external-control-{uuid4().hex}"
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--collect",
            f"--unit={unit}",
            "--property=NoNewPrivileges=yes",
            "--property=RestrictSUIDSGID=yes",
            "/bin/sleep",
            "30",
        ],
        check=True,
    )
    try:
        pid = int(
            subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        controller = LinuxExternalProcessController(
            wait_timeout_seconds=5, curated_process_names=frozenset({"sleep"})
        )
        identity = controller.identify(pid)
        assert identity is not None

        result = await controller.terminate(identity)

        assert result.outcome is ExternalStopOutcome.EXITED
    finally:
        await asyncio.to_thread(
            subprocess.run,
            ["systemctl", "--user", "stop", unit],
            check=False,
        )
