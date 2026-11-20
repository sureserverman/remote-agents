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
import unicodedata
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
#: The most a record of the running daemon's process can weigh (measured 2026-11-12 on
#: 0.167.2: 434 bytes). Which file holds it is `_daemon_pid_file`'s.
_DAEMON_PID_MAX_BYTES = 64 * 1024
#: Codex's own artifacts under `<CODEX_HOME>/app-server-daemon`, the daemon-owned names first.
_DAEMON_OWNED_ARTIFACTS = (
    "daemon.pid",
    "daemon.stderr.log",
    "daemon-updater.pid",
    "daemon-updater.stderr.log",
)
_STANDALONE_ARTIFACTS = (
    "app-server.pid",
    "app-server.stderr.log",
    "app-server-updater.pid",
    "app-server-updater.stderr.log",
)
_CONTINUATION, _TEXT, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x8, 0x9, 0xA

#: The client this adapter says it is, in `initialize`. The daemon logs it; it is not a secret.
_CLIENT_INFO = MappingProxyType({"name": "remote-agents", "version": "1"})


class _Closed(Exception):
    """The daemon closed the connection, or broke the WebSocket framing."""


class _Refused(Exception):
    """The peer on the socket did not complete a WebSocket upgrade this client can verify."""


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
    first line that keeps only plain words and fails closed (`_excerpt`, owner OD-9). A thread
    or method the daemon lacks is said in this adapter's words. Nothing else the daemon printed
    comes back out (`tests/architecture/test_every_parser_refuses_to_echo.py`).

    `apply` with `provider_default` is a successful no-op: that preset means "add nothing".

    **Which daemon.** A restarted daemon has forgotten a thread's settings, so the caller
    re-applies when `server_identity` changes. It is a digest of the process fields in the pid
    file Codex writes under `CODEX_HOME`, read without talking to the daemon.
    """

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        home: Path | None = None,
    ) -> None:
        self._runner: CommandRunner = runner or AsyncCommandRunner()
        self._timeout = timeout
        self._home = home

    async def apply(
        self,
        thread_id: str,
        preset: PermissionPreset,
        *,
        writable_roots: tuple[Path, ...] = (),
    ) -> None:
        """Set `thread_id`'s settings to `preset`'s. `thread_id` is the service's own binding.

        `writable_roots` go only into a `workspaceWrite` sandbox, as its `writableRoots`, and
        only as one handoff folder (`_grantable`): the owner granted a cross-project successor its
        source's `.claude/handoffs` and nothing else (BL-144, DEC-139: on the thread, never as a
        launch flag). Full access has no sandbox to widen and ignores them.
        """
        try:
            params = CODEX_THREAD_SETTINGS.get(PermissionPreset(preset))
        except ValueError:
            raise ThreadPermissionsError("unknown permission preset") from None
        if params is None:
            return
        if len(writable_roots) > 1:
            raise ThreadPermissionsError("more than one writable root")
        for root in writable_roots:
            if not _grantable(root):
                raise ThreadPermissionsError("a writable root that is not a handoff folder")
        update = {"threadId": thread_id, **_plain(params)}
        sandbox = update.get("sandboxPolicy")
        if writable_roots and isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite":
            update["sandboxPolicy"] = {
                **sandbox,
                "writableRoots": [str(root) for root in writable_roots],
            }
        await self._session(thread_id, (("thread/settings/update", update),))

    async def server_identity(self) -> str | None:
        """Which daemon process serves this `CODEX_HOME`, or None when no record says.

        Every failure is None, never a guess: a caller that cannot tell whether the daemon
        changed leaves an applied preset alone. One stat and one read of a few hundred bytes,
        so it stays on the loop, as `CodexHomeSettings` does.
        """
        record = _daemon_record(self._home)
        if record is None:
            return None
        pid = record.get("pid")
        started = record.get("processStartTime")
        process = record.get("processIdentity")
        boot = process.get("bootId") if isinstance(process, dict) else None
        ticks = process.get("startTicks") if isinstance(process, dict) else None
        started = started if isinstance(started, str) else None
        boot = boot if isinstance(boot, str) else None
        ticks = ticks if isinstance(ticks, int) and not isinstance(ticks, bool) else None
        # A pid alone is reused; it names a process only together with when it started.
        if not isinstance(pid, int) or isinstance(pid, bool) or (started is None and ticks is None):
            return None
        fields = json.dumps([pid, started, boot, ticks])
        return hashlib.sha256(fields.encode()).hexdigest()[:32]

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
        except _Refused:
            raise ThreadPermissionsError(
                "the Codex daemon socket did not speak WebSocket"
            ) from None
        except (_Closed, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            raise ThreadPermissionsError("the Codex daemon closed the connection") from None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                # Bounded on its own: this runs after the deadline has fired, too.
                await asyncio.wait_for(writer.wait_closed(), 1.0)


def _daemon_record(home: Path | None) -> dict[str, object] | None:
    """The JSON object in this home's daemon pid file, or None for anything less.

    `home=None` resolves as `codex` does, so a drill's private home reads its own daemon. One
    descriptor, never following a link: what is checked is what is read. Every failure is None.
    """
    try:
        if home is None:
            configured = os.environ.get("CODEX_HOME")
            home = Path(configured).expanduser() if configured else Path.home() / ".codex"
        descriptor = os.open(
            _daemon_pid_file(home),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except (OSError, RuntimeError, ValueError):
        # RuntimeError: a `~user` home that cannot be expanded.
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
            return None
        raw = os.read(descriptor, _DAEMON_PID_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > _DAEMON_PID_MAX_BYTES:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def daemon_pid(home: Path | None = None, *, proc: Path = Path("/proc")) -> int | None:
    """The pid of this home's running daemon, or None unless the record names a live process.

    For `doctor` (BL-147): the daemon runs every turn, so its AppArmor label decides whether
    Codex can start its sandbox, and it keeps that label across a tmux restart. A pid alone is
    reused, so the record's boot id and start ticks must match the process now holding it.
    """
    record = _daemon_record(home)
    if record is None:
        return None
    pid = record.get("pid")
    process = record.get("processIdentity")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(process, dict):
        return None
    boot, ticks = process.get("bootId"), process.get("startTicks")
    if not isinstance(boot, str) or not isinstance(ticks, int) or isinstance(ticks, bool):
        return None
    try:
        current_boot = (proc / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
        stat_line = (proc / str(pid) / "stat").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    # Field 2, the command name, may hold spaces and `)`; field 22 counts from after the last.
    after_name = stat_line.rpartition(")")[2].split()
    if current_boot != boot or len(after_name) < 20 or after_name[19] != str(ticks):
        return None
    return pid


def _daemon_pid_file(home: Path) -> Path:
    """The pid file Codex keeps for this home's daemon, chosen by Codex's own rule.

    Only a managed binary under `packages/standalone` keeps the legacy `app-server.pid`; a
    daemon that owns its package writes `daemon.pid` (codex 0.168-0.169,
    `app-server-daemon/src/lib.rs` `Daemon::from_environment` and `managed_install.rs`
    `package_root`). The owner's home is the first and the drill's the second (measured
    2026-11-13), and reading only the legacy name left every daemon-owned install with no
    reading, so a restart never re-applied its presets. As in Codex, anything there but a
    missing file counts as present.
    """
    state = home / "app-server-daemon"

    def present(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    if present(home / "packages" / "app-server-daemon" / "current"):
        return state / "daemon.pid"
    if any(present(state / name) for name in _DAEMON_OWNED_ARTIFACTS):
        return state / "daemon.pid"
    if any(present(state / name) for name in _STANDALONE_ARTIFACTS):
        return state / "app-server.pid"
    return state / "daemon.pid"


def _grantable(root: Path) -> bool:
    """Whether `root` may be granted: an absolute `<somewhere>/.claude/handoffs`, no control
    character in it, no `..` in it. The one folder the owner's BL-144 ruling grants."""
    text = str(root)
    return (
        root.is_absolute()
        and root.parts[-2:] == (".claude", "handoffs")
        and ".." not in root.parts
        and not any(unicodedata.category(ch).startswith("C") for ch in text)
    )


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
        raise _Refused


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
    received = 0
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
        if length + received > _MAX_MESSAGE_BYTES:
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
        if parts is None:
            parts = []
        parts.append(data)
        received += len(data)
        if final:
            return _json_object(b"".join(parts))


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
            raise ThreadPermissionsError(
                _refusal(message["error"], method, thread_id),
                not_loaded=_thread_not_found(message["error"], thread_id),
            )
        return message["result"]


#: The only words a cleaned excerpt may keep. An allowlist, not a list of secret shapes: every
#: denylist tried here (2026-11-12) let something through -- a zero-width space splitting a token,
#: a compound key name, a quoted value, base64, a relative path. A word is kept only if it is
#: plainly a word: up to 14 ASCII letters, lower case after an optional capital, with a short
#: apostrophe ending allowed; a single letter, an acronym of 2-4 capitals or a number of up to 3
#: digits, never two of these in a row; the refused method's own name; or a
#: backtick-quoted field name in lowerCamelCase or snake_case with short parts, as Codex quotes
#: `threadId` or `approval_policy` (so `dXNlcjpwYXNz`, base64, cannot pass). Each may open with
#: a bracket and end in plain punctuation. Anything else -- digits mixed with letters, a hyphen,
#: `=`, `@`, a slash, any non-ASCII or invisible character -- becomes `…`.
_KEPT_WORD = re.compile(
    r"""\(?(?:
        (?P<word>[aAI]|[A-Za-z][a-z]{1,13}(?:'[a-z]{1,2})?)
      | (?P<code>[A-Z]{2,4}|\d{1,3}|[b-zB-HJ-Z])
      | `[a-z]{1,14}(?:[A-Z][a-z]{1,14}){0,3}`
      | `[a-z]{1,14}(?:_[a-z]{1,14}){1,3}`
    )[.,;:!?)]*""",
    re.VERBOSE,
)
#: A word naming a credential drops the whole excerpt: what follows it is the likeliest secret,
#: and no rule can tell `token abc` from `token expired`. The long names are also looked for
#: with the spaces taken out, so `pa ss word` and `tok en` count.
_CREDENTIAL_WORDS = re.compile(r"(?i)\b(?:pin|otp|ssh|key|pass|seed|salt|jwt|auth\w*|login|code)\b")
_CREDENTIAL_SPELLED = re.compile(
    r"(?i)token|secret|passw|passphrase|passcode|pwd|credential|apikey|api_key|private|bearer|"
    r"cookie|session|mnemonic|recovery"
)
_EXCERPT_CHARS = 120


def _excerpt(text: object, method: str) -> str | None:
    """A short excerpt of the daemon's own message that can hold no secret, or None.

    The owner chose to see what Codex said when it refuses a change (2026-11-12, OD-9), against
    this project's habit of never echoing provider output. So the excerpt fails closed: the
    first non-blank line, normalised (NFKC), split on whitespace, each word kept only if it
    matches `_KEPT_WORD` (or is the refused method's own name) and `…` otherwise, never two short
    codes in a row (so a number or a pairing code cannot be spelled out in pieces), nothing kept
    at all if the line names a credential, and the result bounded. A character the allowlist does
    not know poisons its whole word rather than being deleted, so it never joins two halves.

    **Accepted residual** (reviewed 2026-11-12): a secret written as ordinary short words, such as
    a passphrase or a user name in plain prose, cannot be told from an error message and would
    pass. It needs the owner's own daemon, whose socket is checked to be the owner's, to echo it.
    """
    if not isinstance(text, str):
        return None
    lines = [line for line in unicodedata.normalize("NFKC", text).splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[0]
    if _CREDENTIAL_WORDS.search(line) or _CREDENTIAL_SPELLED.search("".join(line.split())):
        return None
    words: list[str] = []
    after_code = False
    for word in line.split():
        match = _KEPT_WORD.fullmatch(word) if word.isascii() else None
        kept = match is not None or word.strip(".,;:!?()`") == method
        is_code = match is not None and match.group("code") is not None
        if kept and is_code and after_code:
            kept = False
        after_code = is_code  # a rejected code still counts: no code may follow it
        if not kept:
            if words and words[-1] == "…":
                continue
            word = "…"
        words.append(word)
    excerpt = " ".join(words)
    if len(excerpt) > _EXCERPT_CHARS:
        excerpt = excerpt[: _EXCERPT_CHARS - 1].rsplit(" ", 1)[0] + " …"
    return excerpt if any(word != "…" for word in excerpt.split()) else None


def _thread_not_found(error: object, thread_id: str | None) -> bool:
    """Whether the daemon refused because it does not have the caller's thread loaded."""
    text = error.get("message") if isinstance(error, dict) else None
    return isinstance(text, str) and text.startswith("thread not found") and thread_id is not None


def _refusal(error: object, method: str, thread_id: str | None) -> str:
    """Say what a refusal means: the method, the caller's thread, the code and a cleaned excerpt.

    The thread is always the caller's own id, never one read from the reply.
    """
    text = error.get("message") if isinstance(error, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    subject = "" if thread_id is None else f" for thread {thread_id}"
    if _thread_not_found(error, thread_id):
        return f"thread {thread_id} is not loaded on the Codex daemon"
    if isinstance(text, str) and "unknown variant" in text and method in text:
        reason = f"this Codex does not offer {method}{subject}"
    else:
        what = "the parameters of " if code == -32602 else ""
        reason = f"the Codex daemon refused {what}{method}{subject}"
    numbered = isinstance(code, int) and not isinstance(code, bool)
    if numbered:
        reason += f" (error {code})"
    said = _excerpt(text, method)
    return f"{reason}: Codex said “{said}”" if said else reason
