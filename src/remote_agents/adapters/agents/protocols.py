"""Minimal JSON-RPC process boundary for the experimental Codex app server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from remote_agents.ports.provider_errors import ProviderUnavailable

#: How long a closing app-server child gets to exit on its own, then on SIGTERM, before
#: it is killed. Both are short because every caller of `close()` is shutting down.
_GRACEFUL_EXIT_SECONDS = 2
_TERMINATE_EXIT_SECONDS = 2


class ProtocolError(ProviderUnavailable):
    """Raised when a provider protocol is unavailable or returns an invalid response.

    Subclasses `ports.provider_errors.ProviderUnavailable` so an application service can
    catch the category without importing an adapter (ARCH-02). Every existing `raise` and
    `except ProtocolError` is unaffected -- this widens what can catch it, not what it is.
    """


class JsonRpcProcess:
    """Maintain one argv-only JSON-RPC app-server session until its owner closes it."""

    def __init__(self, argv: tuple[str, ...]) -> None:
        if not argv:
            raise ValueError("protocol command must not be empty")
        self._argv = argv
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        async with self._lock:
            await self._ensure_started()
            return await self._request(method, params)

    @property
    def child_pid(self) -> int | None:
        """The running child's pid, or `None` when none is held -- for callers and tests.

        Public so that "is a child alive, and is it the same one" can be asked without
        reaching for `_process`, which is this class's to rename.
        """
        process = self._process
        if process is None or process.returncode is not None:
            return None
        return process.pid

    async def close(self) -> None:
        """Close only the adapter-owned app-server process.

        Cancellation-safe: the reference is let go of before the wait, so a caller that
        bounds this with `wait_for` and gives up mid-wait would otherwise leave a live child
        that nothing can reclaim. A cancellation that lands during the wait kills the child
        first and then propagates.
        """
        async with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            if process.stdin is not None:
                process.stdin.close()
                await process.stdin.wait_closed()
            try:
                await self._reap(process)
            except asyncio.CancelledError:
                _kill(process)
                raise

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(process.wait(), timeout=_GRACEFUL_EXIT_SECONDS)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_TERMINATE_EXIT_SECONDS)
            except TimeoutError:
                # `terminate()` is SIGTERM, which a child is free to ignore -- and this
                # runs in a shutdown path. An unbounded wait here turns "stop the
                # service" into a hang, systemd's TimeoutStopSec then kills only the
                # main process, and the child this function exists to reclaim is
                # orphaned anyway. Escalating costs one signal and removes both.
                process.kill()
                await process.wait()

    async def _ensure_started(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._request(
            "initialize",
            {"clientInfo": {"name": "remote-agents", "version": "0.2.1"}, "capabilities": {}},
        )
        await self._notify("initialized", {})

    async def _request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        process = self._require_process()
        request_id = self._next_id
        self._next_id += 1
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        )
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
            except TimeoutError as error:
                raise ProtocolError("provider protocol response timed out") from error
            if not line:
                raise ProtocolError("provider protocol closed before responding")
            try:
                response = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if response.get("id") != request_id:
                continue
            result = response.get("result")
            if isinstance(result, dict):
                return result
            raise ProtocolError("provider protocol returned no object result")

    async def _notify(self, method: str, params: Mapping[str, object]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def _write(self, payload: Mapping[str, object]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise ProtocolError("provider protocol stdin is unavailable")
        process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        await process.stdin.drain()

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.stdout is None:
            raise ProtocolError("provider protocol is unavailable")
        return self._process


def _kill(process: asyncio.subprocess.Process) -> None:
    """SIGKILL a child that is still running; a child already gone is not an error here."""
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
