"""Exact Linux implementation of the one permitted external-process signal."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import psutil

from remote_agents.domain.external_sessions import (
    ExternalProcessControlCapability,
    ExternalProcessIdentity,
    ExternalStopOutcome,
    ExternalStopResult,
)


class LinuxExternalProcessController:
    """Revalidate one same-UID process and send only SIGTERM through a stable identity."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        wait_timeout_seconds: float = 5.0,
        effective_uid: int | None = None,
    ) -> None:
        self._proc_root = proc_root
        self._wait_timeout_seconds = wait_timeout_seconds
        self._effective_uid = os.geteuid() if effective_uid is None else effective_uid

    @property
    def capability(self) -> ExternalProcessControlCapability:
        return ExternalProcessControlCapability(
            pidfd_available=_pidfd_supported(), psutil_available=True
        )

    def identify(self, pid: int) -> ExternalProcessIdentity | None:
        """Read bounded metadata needed for a later exact revalidation."""
        try:
            process = psutil.Process(pid)
            effective_uid = process.uids().effective
            name = process.name()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return None
        start_ticks = _start_ticks(self._proc_root / str(pid))
        if start_ticks is None:
            return None
        try:
            return ExternalProcessIdentity(pid, start_ticks, effective_uid, name)
        except ValueError:
            return None

    async def terminate(self, identity: ExternalProcessIdentity) -> ExternalStopResult:
        return await asyncio.to_thread(self._terminate, identity)

    def _terminate(self, identity: ExternalProcessIdentity) -> ExternalStopResult:
        if not self._matches(identity):
            return ExternalStopResult(ExternalStopOutcome.IDENTITY_CHANGED)
        try:
            process = psutil.Process(identity.pid)
            if _pidfd_supported():
                self._terminate_with_pidfd(identity, process)
            else:
                process.terminate()
            process.wait(timeout=self._wait_timeout_seconds)
        except psutil.NoSuchProcess:
            return ExternalStopResult(ExternalStopOutcome.EXITED)
        except psutil.TimeoutExpired:
            return ExternalStopResult(ExternalStopOutcome.TIMED_OUT)
        except (psutil.AccessDenied, PermissionError):
            return ExternalStopResult(ExternalStopOutcome.PERMISSION_DENIED)
        except (OSError, ProcessLookupError):
            return ExternalStopResult(ExternalStopOutcome.IDENTITY_CHANGED)
        return ExternalStopResult(ExternalStopOutcome.EXITED)

    def _terminate_with_pidfd(
        self, identity: ExternalProcessIdentity, process: psutil.Process
    ) -> None:
        """Pin the exact process before the fixed signal; never fall back after a pidfd failure."""
        pidfd_open = os.pidfd_open
        send_signal = signal.pidfd_send_signal
        pidfd = pidfd_open(identity.pid, 0)
        try:
            if not self._matches(identity):
                raise ProcessLookupError(identity.pid)
            send_signal(pidfd, signal.SIGTERM, None, 0)
            # Keep psutil's PID-plus-create-time wait behaviour as the bounded observer.
            process.wait(timeout=0)
        except psutil.TimeoutExpired:
            return
        finally:
            os.close(pidfd)

    def _matches(self, identity: ExternalProcessIdentity) -> bool:
        if identity.effective_uid != self._effective_uid:
            return False
        try:
            process = psutil.Process(identity.pid)
            return (
                process.uids().effective == identity.effective_uid
                and process.name() == identity.process_name
                and _start_ticks(self._proc_root / str(identity.pid)) == identity.start_ticks
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return False


def _pidfd_supported() -> bool:
    return hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


def _start_ticks(directory: Path) -> int | None:
    """Read only `/proc/<pid>/stat` field 22 without opening command or environment data."""
    try:
        value = (directory / "stat").read_text(encoding="utf-8")
        fields = value.rsplit(") ", maxsplit=1)[1].split()
        return int(fields[19])
    except (IndexError, OSError, ValueError):
        return None
