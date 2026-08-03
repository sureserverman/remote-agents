"""The Linux controller can terminate only one exact disposable process."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess

from remote_agents.adapters.processes.linux_control import LinuxExternalProcessController
from remote_agents.domain.external_sessions import (
    ExternalProcessIdentity,
    ExternalStopOutcome,
)


def test_linux_controller_uses_pidfd_when_the_runtime_feature_probe_allows_it(monkeypatch) -> None:
    controller = LinuxExternalProcessController()
    identity = ExternalProcessIdentity(42, 9, 1000, "claude")
    read_fd, write_fd = os.pipe()
    signals: list[tuple[int, int, object, int]] = []

    class Process:
        def wait(self, *, timeout: float) -> None:
            assert timeout == 0

    monkeypatch.setattr(controller, "_matches", lambda _identity: True)
    monkeypatch.setattr(os, "pidfd_open", lambda _pid, _flags: read_fd, raising=False)
    monkeypatch.setattr(
        "remote_agents.adapters.processes.linux_control.signal.pidfd_send_signal",
        lambda fd, sig, info, flags: signals.append((fd, sig, info, flags)),
        raising=False,
    )
    try:
        controller._terminate_with_pidfd(identity, Process())
    finally:
        os.close(write_fd)

    assert signals == [(read_fd, signal.SIGTERM, None, 0)]


async def test_linux_controller_terminates_an_exact_same_uid_helper() -> None:
    helper = subprocess.Popen(["/bin/sleep", "30"])
    controller = LinuxExternalProcessController(wait_timeout_seconds=1)
    try:
        identity = controller.identify(helper.pid)
        assert identity is not None

        result = await controller.terminate(identity)

        assert result.outcome is ExternalStopOutcome.EXITED
    finally:
        if helper.poll() is None:
            helper.terminate()
            await asyncio.to_thread(helper.wait)


async def test_linux_controller_refuses_a_changed_identity_without_signalling() -> None:
    helper = subprocess.Popen(["/bin/sleep", "30"])
    controller = LinuxExternalProcessController(wait_timeout_seconds=1)
    try:
        identity = controller.identify(helper.pid)
        assert identity is not None
        stale = ExternalProcessIdentity(
            identity.pid,
            identity.start_ticks + 1,
            identity.effective_uid,
            identity.process_name,
        )

        result = await controller.terminate(stale)

        assert result.outcome is ExternalStopOutcome.IDENTITY_CHANGED
        assert helper.poll() is None
    finally:
        if helper.poll() is None:
            helper.terminate()
            await asyncio.to_thread(helper.wait)
