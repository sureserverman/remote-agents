"""The Linux controller can terminate only one exact disposable process."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from types import SimpleNamespace

from remote_agents.adapters.processes.linux_control import LinuxExternalProcessController
from remote_agents.domain.external_sessions import (
    ExternalProcessIdentity,
    ExternalStopOutcome,
)


def test_linux_controller_uses_pidfd_when_the_runtime_feature_probe_allows_it(monkeypatch) -> None:
    controller = LinuxExternalProcessController(wait_timeout_seconds=1)
    identity = ExternalProcessIdentity(42, 9, 1000, "claude")
    read_fd, write_fd = os.pipe()
    signals: list[tuple[int, int, object, int]] = []

    class Process:
        def wait(self, *, timeout: float) -> None:
            assert timeout == 0

    monkeypatch.setattr(controller, "_matches", lambda _identity, _process: True)
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
    controller = LinuxExternalProcessController(
        wait_timeout_seconds=1, curated_process_names=frozenset({"sleep"})
    )
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
    controller = LinuxExternalProcessController(
        wait_timeout_seconds=1, curated_process_names=frozenset({"sleep"})
    )
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


def test_linux_controller_anchors_fallback_mutation_to_the_revalidated_process_object(
    monkeypatch,
) -> None:
    controller = LinuxExternalProcessController(wait_timeout_seconds=1)
    created: list[object] = []

    class Process:
        def __init__(self, name: str) -> None:
            self.name_value = name
            self.terminated = False

        def is_running(self) -> bool:
            return True

        def uids(self):
            return SimpleNamespace(effective=1000)

        def name(self) -> str:
            return "claude"

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> None:
            assert timeout == 1

    original = Process("original")
    replacement = Process("replacement")

    def process_for_pid(_pid: int):
        created.append(object())
        return original if len(created) == 1 else replacement

    monkeypatch.setattr(
        "remote_agents.adapters.processes.linux_control.psutil.Process", process_for_pid
    )
    monkeypatch.setattr(
        "remote_agents.adapters.processes.linux_control._start_ticks", lambda _path: 9
    )
    monkeypatch.setattr(
        "remote_agents.adapters.processes.linux_control._pidfd_supported", lambda: False
    )

    result = controller._terminate(ExternalProcessIdentity(42, 9, 1000, "claude"))

    assert result.outcome is ExternalStopOutcome.EXITED
    assert original.terminated is True
    assert replacement.terminated is False


def test_linux_controller_rejects_an_unapproved_process_name_by_default() -> None:
    controller = LinuxExternalProcessController()

    identity = controller.identify(os.getpid())

    assert identity is None
