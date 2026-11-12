"""Codex's preset table, and the descriptor that publishes it (Task 1.2)."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from remote_agents.adapters.agents.codex.thread_settings import (
    CODEX_THREAD_SETTINGS,
    CodexThreadPermissions,
)
from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.domain.profiles import PermissionPreset

#: The exact params each preset sends. Pinned in full: this is a security policy, and the
#: approve-for-me row is the Codex CLI's own expansion of its flag (codex-rs/cli/src/main.rs).
EXPECTED = {
    PermissionPreset.APPROVE_FOR_ME: {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
        "sandboxPolicy": {"type": "workspaceWrite"},
    },
    PermissionPreset.ASK: {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "workspaceWrite"},
    },
    PermissionPreset.FULL_ACCESS: {
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    },
}


def _plain(value):
    if isinstance(value, MappingProxyType | dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def test_the_table_sets_exactly_the_pinned_params_per_preset() -> None:
    assert {preset: _plain(params) for preset, params in CODEX_THREAD_SETTINGS.items()} == EXPECTED


def test_the_table_has_no_row_for_the_provider_default() -> None:
    assert PermissionPreset.PROVIDER_DEFAULT not in CODEX_THREAD_SETTINGS


def test_the_table_cannot_be_edited_at_runtime() -> None:
    with pytest.raises(TypeError):
        CODEX_THREAD_SETTINGS[PermissionPreset.ASK] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        CODEX_THREAD_SETTINGS[PermissionPreset.ASK]["approvalPolicy"] = "never"  # type: ignore[index]


def test_the_table_is_published_only_by_the_codex_descriptor() -> None:
    declared = {
        str(descriptor.profile_id): descriptor.thread_permissions
        for descriptor in provider_descriptors()
    }
    assert isinstance(declared.pop("codex"), CodexThreadPermissions)
    assert set(declared.values()) == {None}


# --- The daemon client (Task 1.3) -------------------------------------------------------------
#
# A real WebSocket server on a real unix socket, inside the test: the client's handshake, masking
# and framing are what is under test, so nothing below the socket is faked. The socket lives in a
# short `/tmp` folder because a pytest `tmp_path` can pass the 107-byte limit on socket paths.

import asyncio  # noqa: E402
import base64  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from remote_agents.adapters.agents.codex.remote_control import CommandResult  # noqa: E402
from remote_agents.ports.thread_permissions import ThreadPermissionsError  # noqa: E402

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
THREAD = "01a1cfbe-ba7d-7050-8760-1563efb05e5f"
POISON = "/home/operator/.codex/auth.json token=sk-abcdef0123456789"


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, timeout):
        self.calls.append(argv)
        return self.result


def _version(socket_path: Path) -> CommandResult:
    payload = {"status": "running", "socketPath": str(socket_path), "appServerVersion": "0.167.2"}
    return CommandResult(0, json.dumps(payload), "")


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes, bool]:
    head = await reader.readexactly(2)
    opcode, masked, length = head[0] & 0x0F, bool(head[1] & 0x80), head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b"\0\0\0\0"
    data = await reader.readexactly(length)
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data)), masked


def _frame(payload: bytes, opcode: int = 1) -> bytes:
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + struct.pack(">H", n) + payload
    return bytes([0x80 | opcode, 127]) + struct.pack(">Q", n) + payload


class FakeDaemon:
    """A WebSocket JSON-RPC peer. `answer(message)` returns the reply to send, or None for none."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.received: list[dict] = []
        self.unmasked = False
        self.connections = 0
        self.closed = asyncio.Event()
        self.folder = Path(tempfile.mkdtemp(prefix="ra-ws-", dir="/tmp"))
        self.path = self.folder / "s.sock"

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()
        shutil.rmtree(self.folder, ignore_errors=True)

    async def _serve(self, reader, writer):
        self.connections += 1
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            key = next(
                line.split(b":", 1)[1].strip()
                for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key:")
            )
            accept = base64.b64encode(hashlib.sha1(key + _GUID.encode()).digest())
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            await writer.drain()
            while True:
                opcode, data, masked = await _read_frame(reader)
                self.unmasked |= not masked
                if opcode == 8:
                    break
                message = json.loads(data)
                self.received.append(message)
                for reply in self.answer(message) or ():
                    if reply == "close":
                        writer.close()
                        return
                    writer.write(_frame(json.dumps(reply).encode()))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed.set()
            with contextlib.suppress(Exception):
                writer.close()


def _cooperative(message):
    """Answer like the real daemon, with a notification first and a reply past 125 bytes."""
    if "id" not in message:
        return ()
    note = {"method": "thread/status/changed", "params": {"threadId": "someone-else"}}
    result = (
        {"userAgent": "codex/0.167.2 " + "x" * 200} if message["method"] == "initialize" else {}
    )
    return (note, {"id": message["id"], "result": result})


def _client(
    socket_path: Path, *, timeout: float = 5.0
) -> tuple[CodexThreadPermissions, FakeRunner]:
    runner = FakeRunner(_version(socket_path))
    return CodexThreadPermissions(runner=runner, timeout=timeout), runner


async def test_apply_initializes_with_experimental_api_then_updates_the_thread() -> None:
    async with FakeDaemon(_cooperative) as daemon:
        client, runner = _client(daemon.path)
        await client.apply(THREAD, PermissionPreset.APPROVE_FOR_ME)
        await asyncio.wait_for(daemon.closed.wait(), 2)
    assert runner.calls == [("codex", "app-server", "daemon", "version")]
    methods = [message["method"] for message in daemon.received]
    assert methods == ["initialize", "initialized", "thread/settings/update"]
    assert daemon.received[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert daemon.received[2]["params"] == {
        "threadId": THREAD,
        **EXPECTED[PermissionPreset.APPROVE_FOR_ME],
    }
    assert daemon.unmasked is False


@pytest.mark.parametrize("preset", [PermissionPreset.ASK, PermissionPreset.FULL_ACCESS])
async def test_apply_sends_each_presets_pinned_params(preset) -> None:
    async with FakeDaemon(_cooperative) as daemon:
        client, _ = _client(daemon.path)
        await client.apply(THREAD, preset)
    assert daemon.received[-1]["params"] == {"threadId": THREAD, **EXPECTED[preset]}


async def test_a_long_frame_from_the_client_uses_the_16_bit_length() -> None:
    long_thread = "t" * 200
    async with FakeDaemon(_cooperative) as daemon:
        client, _ = _client(daemon.path)
        await client.apply(long_thread, PermissionPreset.ASK)
    assert daemon.received[-1]["params"]["threadId"] == long_thread


async def test_the_provider_default_makes_no_call_and_no_connection() -> None:
    async with FakeDaemon(_cooperative) as daemon:
        client, runner = _client(daemon.path)
        await client.apply(THREAD, PermissionPreset.PROVIDER_DEFAULT)
    assert runner.calls == [] and daemon.connections == 0


async def test_no_daemon_running_is_named_and_starts_nothing() -> None:
    runner = FakeRunner(
        CommandResult(1, "", "Error: connect app-server-control.sock: No such file or directory")
    )
    with pytest.raises(ThreadPermissionsError) as raised:
        await CodexThreadPermissions(runner=runner).apply(THREAD, PermissionPreset.ASK)
    assert raised.value.reason == "no Codex daemon"
    assert runner.calls == [("codex", "app-server", "daemon", "version")]


async def test_an_unreadable_version_reading_never_echoes_it() -> None:
    runner = FakeRunner(CommandResult(0, POISON, POISON))
    with pytest.raises(ThreadPermissionsError) as raised:
        await CodexThreadPermissions(runner=runner).apply(THREAD, PermissionPreset.ASK)
    assert "sk-abcdef" not in raised.value.reason and "auth.json" not in raised.value.reason
    assert "sk-abcdef" not in repr(raised.value)


async def test_an_unloaded_thread_is_named_by_the_callers_own_id() -> None:
    def refuse(message):
        if message.get("method") == "thread/settings/update":
            error = {"code": -32600, "message": f"thread not found: {THREAD}"}
            return ({"id": message["id"], "error": error},)
        return _cooperative(message)

    async with FakeDaemon(refuse) as daemon:
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
    assert THREAD in raised.value.reason and "not loaded" in raised.value.reason


async def test_an_unknown_method_says_this_codex_does_not_offer_it_and_echoes_nothing() -> None:
    def refuse(message):
        if message.get("method") == "thread/settings/update":
            text = "Invalid request: unknown variant `thread/settings/update`, " + POISON * 100
            return ({"id": message["id"], "error": {"code": -32600, "message": text}},)
        return _cooperative(message)

    async with FakeDaemon(refuse) as daemon:
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
    reason = raised.value.reason
    assert "does not offer" in reason and len(reason) <= 160
    assert "sk-abcdef" not in reason and "auth.json" not in reason


async def test_any_other_refusal_carries_only_its_code() -> None:
    def refuse(message):
        if message.get("method") == "thread/settings/update":
            return ({"id": message["id"], "error": {"code": -32602, "message": POISON}},)
        return _cooperative(message)

    async with FakeDaemon(refuse) as daemon:
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
    assert "-32602" in raised.value.reason and "sk-abcdef" not in raised.value.reason


async def test_a_daemon_that_never_answers_times_out_within_the_bound_and_is_hung_up_on() -> None:
    async with FakeDaemon(lambda message: ()) as daemon:
        client, _ = _client(daemon.path, timeout=0.5)
        started = time.monotonic()
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
        assert time.monotonic() - started < 2.0
        await asyncio.wait_for(daemon.closed.wait(), 2)
    assert raised.value.reason == "timed out"


async def test_a_daemon_that_hangs_up_is_named() -> None:
    async with FakeDaemon(lambda message: ("close",)) as daemon:
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
    assert raised.value.reason == "the Codex daemon closed the connection"


async def test_a_socket_that_is_gone_is_named() -> None:
    client, _ = _client(Path("/tmp/ra-ws-missing/s.sock"))
    with pytest.raises(ThreadPermissionsError) as raised:
        await client.apply(THREAD, PermissionPreset.ASK)
    assert raised.value.reason == "no Codex daemon"


def test_the_client_adds_no_dependency() -> None:
    pyproject = (Path(__file__).parents[5] / "pyproject.toml").read_text()
    for library in ("websocket", "aiohttp", "wsproto"):
        assert library not in pyproject
