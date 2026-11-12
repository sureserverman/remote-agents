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
import re
import stat
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
        # No reviewer: with `never` nothing is asked, so whichever reviewer the thread held
        # before is never consulted. Pinned as written by the exact-match test.
        PermissionPreset.FULL_ACCESS: MappingProxyType(
            {
                "approvalPolicy": "never",
                "sandboxPolicy": MappingProxyType({"type": "dangerFullAccess"}),
            }
        ),
    }
)


#: Bounds. One apply probes the daemon, opens one connection and makes two requests, all under
#: one deadline; ten seconds covers a cold daemon. A reply larger than a megabyte is not a
#: settings answer.
_TIMEOUT_SECONDS = 10.0
_MAX_MESSAGE_BYTES = 1 << 20
#: Pages of `thread/loaded/list` read before giving up; a repeated cursor stops sooner.
_MAX_PAGES = 100
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CONTINUATION, _TEXT, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x8, 0x9, 0xA

#: The client this adapter says it is, in `initialize`. The daemon logs it; it is not a secret.
_CLIENT_INFO = MappingProxyType({"name": "remote-agents", "version": "1"})


class _Closed(Exception):
    """The daemon closed the connection, or broke the WebSocket framing."""


class _Unexpected(Exception):
    """The daemon answered with something that is not a JSON-RPC reply this client can read."""


class CodexThreadPermissions:
    """The `ThreadPermissions` port over Codex's shared app-server daemon.

    **Where the daemon is.** `codex app-server daemon version` names the control socket of the
    daemon serving this `CODEX_HOME` (`socketPath`), so the client never guesses a path and a
    drill's private home reaches its own daemon. Nothing here starts a daemon: a launch of plain
    `codex` does that, and an apply with none running fails "no Codex daemon". The path must be
    a socket owned by this user: a policy is never sent to somebody else's listener.

    **Transport.** The socket speaks WebSocket (measured 2026-11-12: `HTTP/1.1 101`). The stdlib
    has no client, and one connection sending two text frames does not justify a dependency, so
    the handshake and the masked frames are written out below (RFC 6455 §4-5), strictly: a
    binary, reserved-bit or oversized control frame ends the connection.

    **A reply is only a reply.** A message counts as the answer to a request only when it has no
    `method`, carries that request's integer id, and holds exactly one of `result` and `error`;
    a server-to-client request reusing the id is not mistaken for success.

    **Refusals say what they mean, and what Codex said, cleaned.** A refusal names the method,
    the caller's own thread id and the JSON-RPC code, plus a bounded excerpt of the daemon's own
    first line with every path and secret-shaped piece removed (`_excerpt`, owner OD-9). A thread
    or method the daemon lacks is said in this adapter's words. Nothing else the daemon printed
    comes back out (`tests/architecture/test_every_parser_refuses_to_echo.py`).

    `apply` with `provider_default` is a successful no-op: that preset means "add nothing".
    """

    def __init__(
        self, runner: CommandRunner | None = None, *, timeout: float = _TIMEOUT_SECONDS
    ) -> None:
        self._runner: CommandRunner = runner or AsyncCommandRunner()
        self._timeout = timeout

    async def apply(self, thread_id: str, preset: PermissionPreset) -> None:
        """Set `thread_id`'s settings to `preset`'s. `thread_id` is the service's own binding."""
        try:
            params = CODEX_THREAD_SETTINGS.get(PermissionPreset(preset))
        except ValueError:
            raise ThreadPermissionsError("unknown permission preset") from None
        if params is None:
            return
        await self._session(
            thread_id,
            (("thread/settings/update", {"threadId": thread_id, **_plain(params)}),),
        )

    async def loaded_threads(self) -> frozenset[str]:
        """The ids of every thread loaded on the daemon (`thread/loaded/list`, all pages)."""
        threads: set[str] = set()
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(_MAX_PAGES):
            params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
            (result,) = await self._session(None, (("thread/loaded/list", params),))
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
                raise ThreadPermissionsError("unexpected reply from the Codex daemon")
            threads.update(data)
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return frozenset(threads)
            if cursor in seen:
                break
            seen.add(cursor)
        raise ThreadPermissionsError("unexpected reply from the Codex daemon")

    async def thread_folder(self, thread_id: str) -> Path:
        """The working folder the daemon recorded for `thread_id` (`thread/read`, no turns)."""
        (result,) = await self._session(
            thread_id, (("thread/read", {"threadId": thread_id, "includeTurns": False}),)
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        folder = thread.get("cwd") if isinstance(thread, dict) else None
        if not isinstance(folder, str) or not os.path.isabs(folder):
            raise ThreadPermissionsError("unexpected reply from the Codex daemon")
        return Path(folder)

    async def _session(
        self, thread_id: str | None, calls: tuple[tuple[str, Mapping[str, object]], ...]
    ) -> list[object]:
        """Probe, connect, initialize and make `calls` under one deadline; their results."""
        try:
            async with asyncio.timeout(self._timeout):
                socket_path = await self._socket_path()
                return await self._exchange(socket_path, thread_id, calls)
        except TimeoutError:
            raise ThreadPermissionsError("timed out") from None
        except ThreadPermissionsError:
            raise
        except Exception:
            # Anything else a peer can provoke -- a reply nested deep enough to exhaust the
            # parser, say -- is still this port's one error, never an exception the caller does
            # not handle. Cancellation is not an Exception and still propagates.
            raise ThreadPermissionsError("unexpected reply from the Codex daemon") from None

    async def _socket_path(self) -> Path:
        try:
            result = await self._runner.run(
                REMOTE_CONTROL_ARGV["daemon_probe"], timeout=self._timeout
            )
        except ProtocolError as error:
            # The runner's own two sentences (`AsyncCommandRunner`), not the provider's output.
            if "not installed" in str(error):
                raise ThreadPermissionsError("codex is not installed") from None
            raise ThreadPermissionsError("timed out") from None
        if result.returncode != 0:
            raise ThreadPermissionsError("no Codex daemon")
        path = self._parse_socket_path(result.stdout)
        try:
            status = path.stat()
        except OSError:
            raise ThreadPermissionsError("no Codex daemon") from None
        if not stat.S_ISSOCK(status.st_mode) or status.st_uid != os.getuid():
            raise ThreadPermissionsError("the Codex daemon socket is not this user's")
        return path

    @staticmethod
    def _parse_socket_path(stdout: str) -> Path:
        """`socketPath` out of `daemon version`'s JSON, or "no Codex daemon" -- never its text."""
        try:
            payload = json.loads(stdout)
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict) or payload.get("status") != "running":
            raise ThreadPermissionsError("no Codex daemon")
        path = payload.get("socketPath")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ThreadPermissionsError("no Codex daemon")
        return Path(path)

    async def _exchange(
        self,
        socket_path: Path,
        thread_id: str | None,
        calls: tuple[tuple[str, Mapping[str, object]], ...],
    ) -> list[object]:
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
            return [
                await _call(reader, writer, index, method, params, thread_id)
                for index, (method, params) in enumerate(calls, start=2)
            ]
        except _Unexpected:
            raise ThreadPermissionsError("unexpected reply from the Codex daemon") from None
        except (_Closed, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            raise ThreadPermissionsError("the Codex daemon closed the connection") from None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                # Bounded on its own: this runs after the deadline has fired, too.
                await asyncio.wait_for(writer.wait_closed(), 1.0)


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
    status = lines[0].split(b" ")
    if len(status) < 2 or status[1] != b"101" or answered.get(b"sec-websocket-accept") != accept:
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
    parts: list[bytes] | None = None
    while True:
        first, second = await reader.readexactly(2)
        final, reserved, opcode = bool(first & 0x80), first & 0x70, first & 0x0F
        length = second & 0x7F
        control = opcode >= _CLOSE
        if reserved or (control and (length > 125 or not final)):
            raise _Closed
        if length == 126:
            (length,) = struct.unpack(">H", await reader.readexactly(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", await reader.readexactly(8))
        if length + sum(map(len, parts or ())) > _MAX_MESSAGE_BYTES:
            raise _Closed
        mask = await reader.readexactly(4) if second & 0x80 else None
        data = await reader.readexactly(length)
        if mask is not None:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        if opcode == _CLOSE:
            raise _Closed
        if opcode == _PING:
            writer.write(_frame(data, _PONG))
            await writer.drain()
            continue
        if control:
            continue  # a pong, or a reserved control opcode
        if opcode not in (_CONTINUATION, _TEXT):
            raise _Closed  # 0x3-0x7: reserved data opcodes, never negotiated here
        if (opcode == _CONTINUATION) is (parts is None):
            raise _Closed  # a continuation with nothing to continue, or a text frame mid-message
        parts = [*(parts or ()), data]
        if final:
            try:
                return _json_object(b"".join(parts))
            finally:
                parts = None


def _json_object(raw: bytes) -> dict:
    try:
        message = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _Unexpected from None
    if not isinstance(message, dict):
        raise _Unexpected
    return message


async def _call(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request_id: int,
    method: str,
    params: Mapping[str, object],
    thread_id: str | None,
) -> object:
    """Send one request and return its `result`; a refusal raises in this adapter's words."""
    await _send(writer, {"id": request_id, "method": method, "params": params})
    while True:
        message = await _message(reader, writer)
        reply_id = message.get("id")
        if "method" in message or type(reply_id) is not int or reply_id != request_id:
            continue  # a notification, a request from the daemon, or another request's reply
        if ("result" in message) == ("error" in message):
            raise _Unexpected
        if "error" in message:
            raise ThreadPermissionsError(_refusal(message["error"], method, thread_id))
        return message["result"]


#: What a cleaned excerpt may never carry: an absolute or home path, a `key=value` or `key: value`
#: whose key names a credential, a bearer-style token, any long unbroken run of letters and
#: digits (keys, ids, hashes), and a pairing-code-shaped group (`ABCD-EFGH`).
_SECRET_SHAPES = (
    re.compile(r"(?<![\w.~/])(?:~|\.{1,2})?/[^\s'\"`]*"),
    re.compile(
        r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|key|auth|cookie|bearer|code)"
        r"\b\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b"),
)
_EXCERPT_CHARS = 120


def _excerpt(text: object) -> str | None:
    """A short, cleaned excerpt of the daemon's own message, or None when nothing safe is left.

    The owner chose to see what Codex said when it refuses a change (2026-11-12, OD-9), against
    this project's habit of never echoing provider output. So only the first line is kept, every
    secret-shaped piece is replaced by `…`, anything unprintable is dropped, and the result is
    bounded. The pairing code Remote Control mints is never in a settings reply; its shape is
    stripped anyway.
    """
    if not isinstance(text, str):
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    for shape in _SECRET_SHAPES:
        line = shape.sub("…", line)
    line = "".join(character for character in line if character.isprintable())
    line = re.sub(r"\s+", " ", line).strip(" :")
    if len(line) > _EXCERPT_CHARS:
        line = line[: _EXCERPT_CHARS - 1].rstrip() + "…"
    return line if line.strip("… ") else None


def _refusal(error: object, method: str, thread_id: str | None) -> str:
    """Say what a refusal means: the method, the caller's thread, the code and a cleaned excerpt.

    The thread is always the caller's own id, never one read from the reply.
    """
    text = error.get("message") if isinstance(error, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    subject = "" if thread_id is None else f" for thread {thread_id}"
    if isinstance(text, str) and text.startswith("thread not found") and thread_id is not None:
        return f"thread {thread_id} is not loaded on the Codex daemon"
    if isinstance(text, str) and "unknown variant" in text and method in text:
        return f"this Codex does not offer {method}"
    what = "the parameters of " if code == -32602 else ""
    numbered = isinstance(code, int) and not isinstance(code, bool)
    reason = f"the Codex daemon refused {what}{method}{subject}"
    if numbered:
        reason += f" (error {code})"
    said = _excerpt(text)
    return f"{reason}: Codex said “{said}”" if said else reason
