"""Apply a permission preset to a Codex thread on the shared app-server daemon.

**Why after launch, not at it.** Codex 0.167 runs a TUI started with any command-line config
override -- the permission flags included -- on an embedded app server, off the shared daemon
(`codex-rs/tui/src/daemon_startup.rs`, `exclusion()`), and Codex Remote Control reaches only
threads on that daemon. So a managed Codex launches plain, and the service sets the thread's
permissions here once it knows which thread is the session's (DEC-138).

**What a preset means to Codex**, measured on 0.167.2 against the CLI's own expansion of its
approve-for-me flag (`codex-rs/cli/src/main.rs` tests): approval policy, approval reviewer and
sandbox. `thread/settings/update` takes the three, and a TUI attached to the daemon applies the
`ThreadSettingsUpdated` notification to its active thread (`chatwidget/settings.rs`). The
provider default has no row: it means "add nothing", and the caller sends nothing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import struct
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from remote_agents.adapters.agents.codex.remote_control import (
    REMOTE_CONTROL_ARGV,
    AsyncCommandRunner,
    CommandRunner,
)
from remote_agents.adapters.agents.protocols import ProtocolError
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.thread_permissions import ThreadPermissionsError

_WORKSPACE = MappingProxyType({"type": "workspaceWrite"})

#: Preset -> the `thread/settings/update` params it sets, besides `threadId`. Closed, and pinned
#: by an exact-match test: these are a security policy, so a value is never assembled from input.
CODEX_THREAD_SETTINGS: Mapping[PermissionPreset, Mapping[str, object]] = MappingProxyType(
    {
        PermissionPreset.APPROVE_FOR_ME: MappingProxyType(
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "sandboxPolicy": _WORKSPACE,
            }
        ),
        PermissionPreset.ASK: MappingProxyType(
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": _WORKSPACE,
            }
        ),
        PermissionPreset.FULL_ACCESS: MappingProxyType(
            {
                "approvalPolicy": "never",
                "sandboxPolicy": MappingProxyType({"type": "dangerFullAccess"}),
            }
        ),
    }
)


#: Bounds. One apply opens one connection and makes two requests; ten seconds covers a cold
#: daemon. A reply larger than a megabyte is not a settings answer.
_TIMEOUT_SECONDS = 10.0
_MAX_MESSAGE_BYTES = 1 << 20
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _CONTINUATION, _CLOSE, _PING = 0x1, 0x0, 0x8, 0x9

#: The client this adapter says it is, in `initialize`. The daemon logs it; it is not a secret.
_CLIENT_INFO = MappingProxyType({"name": "remote-agents", "version": "1"})


class _Closed(Exception):
    """The daemon closed the connection, or sent something that ends it."""


class CodexThreadPermissions:
    """The `ThreadPermissions` port over Codex's shared app-server daemon.

    **Where the daemon is.** `codex app-server daemon version` names the control socket of the
    daemon serving this `CODEX_HOME` (`socketPath`), so the client never guesses a path and a
    drill's private home reaches its own daemon. Nothing here starts a daemon: a launch of plain
    `codex` does that, and an apply with none running fails "no Codex daemon".

    **Transport.** The socket speaks WebSocket (measured 2026-11-12: `HTTP/1.1 101`). The stdlib
    has no client, and one connection sending two text frames does not justify a dependency, so
    the handshake and the masked frames are written out below (RFC 6455 §4-5).

    **Refusals say what they mean, never what the daemon printed.** Provider output does not
    come back out of an error (`tests/architecture/test_every_parser_refuses_to_echo.py`): a
    thread the daemon does not have is named by the caller's own id, a method it does not know
    means this Codex does not offer it, and anything else is reported by its JSON-RPC code.
    """

    def __init__(
        self, runner: CommandRunner | None = None, *, timeout: float = _TIMEOUT_SECONDS
    ) -> None:
        self._runner: CommandRunner = runner or AsyncCommandRunner()
        self._timeout = timeout

    async def apply(self, thread_id: str, preset: PermissionPreset) -> None:
        params = CODEX_THREAD_SETTINGS.get(PermissionPreset(preset))
        if params is None:
            return
        socket_path = await self._socket_path()
        try:
            async with asyncio.timeout(self._timeout):
                await self._exchange(socket_path, thread_id, _plain(params))
        except TimeoutError:
            raise ThreadPermissionsError("timed out") from None

    async def _socket_path(self) -> Path:
        try:
            result = await self._runner.run(
                REMOTE_CONTROL_ARGV["daemon_probe"], timeout=self._timeout
            )
        except ProtocolError:
            raise ThreadPermissionsError("codex did not answer") from None
        if result.returncode != 0:
            raise ThreadPermissionsError("no Codex daemon")
        return self._parse_socket_path(result.stdout)

    @staticmethod
    def _parse_socket_path(stdout: str) -> Path:
        """`socketPath` out of `daemon version`'s JSON, or "no Codex daemon" -- never its text."""
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("status") != "running":
            raise ThreadPermissionsError("no Codex daemon")
        path = payload.get("socketPath")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ThreadPermissionsError("no Codex daemon")
        return Path(path)

    async def _exchange(self, socket_path: Path, thread_id: str, params: dict) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
        except OSError:
            raise ThreadPermissionsError("no Codex daemon") from None
        try:
            await _handshake(reader, writer)
            await _call(
                reader,
                writer,
                1,
                "initialize",
                {"clientInfo": dict(_CLIENT_INFO), "capabilities": {"experimentalApi": True}},
                thread_id,
            )
            await _send(writer, {"method": "initialized"})
            await _call(
                reader,
                writer,
                2,
                "thread/settings/update",
                {"threadId": thread_id, **params},
                thread_id,
            )
        except (_Closed, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            raise ThreadPermissionsError("the Codex daemon closed the connection") from None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    key = base64.b64encode(os.urandom(16))
    writer.write(
        b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    accept = base64.b64encode(hashlib.sha1(key + _WEBSOCKET_GUID).digest())
    answered = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(b":") for line in lines[1:])
    }
    if b" 101 " not in lines[0] + b" " or answered.get(b"sec-websocket-accept") != accept:
        raise _Closed


async def _send(writer: asyncio.StreamWriter, message: Mapping[str, object]) -> None:
    writer.write(_frame(json.dumps(message).encode(), _TEXT))
    await writer.drain()


def _frame(payload: bytes, opcode: int) -> bytes:
    """One final, masked client frame (a client must mask every frame, RFC 6455 §5.3)."""
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        head = bytes([0x80 | opcode, 0x80 | length])
    elif length < 1 << 16:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
    return head + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


async def _message(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict:
    """The next whole text message, answering pings and reassembling fragments."""
    parts: list[bytes] = []
    while True:
        first, second = await reader.readexactly(2)
        final, opcode = bool(first & 0x80), first & 0x0F
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", await reader.readexactly(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", await reader.readexactly(8))
        if length + sum(map(len, parts)) > _MAX_MESSAGE_BYTES:
            raise _Closed
        mask = await reader.readexactly(4) if second & 0x80 else None
        data = await reader.readexactly(length)
        if mask is not None:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        if opcode == _CLOSE:
            raise _Closed
        if opcode == _PING:
            writer.write(_frame(data, 0xA))
            await writer.drain()
            continue
        if opcode not in (_TEXT, _CONTINUATION):
            continue
        parts.append(data)
        if final:
            try:
                message = json.loads(b"".join(parts))
            except ValueError:
                raise _Closed from None
            if isinstance(message, dict):
                return message
            parts = []


async def _call(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request_id: int,
    method: str,
    params: Mapping[str, object],
    thread_id: str,
) -> None:
    await _send(writer, {"id": request_id, "method": method, "params": params})
    while True:
        message = await _message(reader, writer)
        if message.get("id") != request_id:
            continue  # a notification, or another client's traffic
        error = message.get("error")
        if error is not None:
            raise ThreadPermissionsError(_refusal(error, method, thread_id))
        return


def _refusal(error: object, method: str, thread_id: str) -> str:
    """Say what a refusal means in this adapter's words; the daemon's text never comes back."""
    text = error.get("message") if isinstance(error, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(text, str) and text.startswith("thread not found"):
        return f"thread {thread_id} is not loaded on the Codex daemon"
    if isinstance(text, str) and "unknown variant" in text and method in text:
        return f"this Codex does not offer {method}"
    if isinstance(code, int) and not isinstance(code, bool):
        return f"the Codex daemon refused {method} (error {code})"
    return f"the Codex daemon refused {method}"
