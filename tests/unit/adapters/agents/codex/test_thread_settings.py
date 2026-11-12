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

    def __init__(self, answer, *, status: bytes = b"101", accept: bytes | None = None) -> None:
        self.answer = answer
        self.status = status
        self.accept = accept
        self.received: list[dict] = []
        self.opcodes: list[int] = []
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
            accept = self.accept or base64.b64encode(hashlib.sha1(key + _GUID.encode()).digest())
            writer.write(
                b"HTTP/1.1 " + self.status + b" Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            await writer.drain()
            while True:
                opcode, data, masked = await _read_frame(reader)
                self.unmasked |= not masked
                self.opcodes.append(opcode)
                if opcode == 8:
                    break
                if opcode != 1:
                    continue
                message = json.loads(data)
                self.received.append(message)
                for reply in self.answer(message) or ():
                    if reply == "close":
                        writer.close()
                        return
                    if isinstance(reply, bytes):
                        writer.write(reply)  # a raw frame, written as the test built it
                    else:
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
    # Said as a fact the caller can act on: the thread may simply not be loaded yet.
    assert raised.value.not_loaded is True


async def test_only_a_missing_thread_is_marked_not_loaded() -> None:
    """Every other refusal, and every transport failure, is a failure: waiting does not fix a
    method this Codex lacks, a refused parameter or a daemon that is not there."""

    def refuse(message):
        if message.get("method") == "thread/settings/update":
            error = {"code": -32602, "message": "invalid params: unknown field"}
            return ({"id": message["id"], "error": error},)
        return _cooperative(message)

    async with FakeDaemon(refuse) as daemon:
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
    assert raised.value.not_loaded is False

    gone = CodexThreadPermissions(runner=FakeRunner(CommandResult(1, "", "")), timeout=1.0)
    with pytest.raises(ThreadPermissionsError) as raised:
        await gone.apply(THREAD, PermissionPreset.ASK)
    assert raised.value.not_loaded is False
    assert ThreadPermissionsError("timed out").not_loaded is False


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


# --- Framing and reply rules the client claims (Stage 1 remediation round 1) -----------------


def _on_update(*replies):
    """Answer `initialize` normally, and the settings update with `replies`."""

    def answer(message):
        if message.get("method") == "thread/settings/update":
            if len(replies) == 1 and callable(replies[0]):
                return replies[0](message["id"])
            return replies
        return _cooperative(message)

    return answer


async def _apply_against(daemon_answer, *, timeout: float = 5.0, **daemon) -> FakeDaemon:
    async with FakeDaemon(daemon_answer, **daemon) as fake:
        client, _ = _client(fake.path, timeout=timeout)
        await client.apply(THREAD, PermissionPreset.ASK)
    return fake


async def _refused(daemon_answer, *, timeout: float = 5.0, **daemon) -> str:
    with pytest.raises(ThreadPermissionsError) as raised:
        await _apply_against(daemon_answer, timeout=timeout, **daemon)
    return raised.value.reason


async def test_a_wrong_websocket_accept_is_refused() -> None:
    reason = await _refused(_cooperative, accept=b"bm90LXRoZS1yaWdodC1hY2NlcHQ=")
    assert reason == "the Codex daemon socket did not speak WebSocket"


async def test_a_reply_that_is_not_101_is_refused() -> None:
    reason = await _refused(_cooperative, status=b"200")
    assert reason == "the Codex daemon socket did not speak WebSocket"


async def test_a_ping_is_answered_with_a_pong_and_the_reply_still_counts() -> None:
    def answer(message):
        if message.get("method") == "thread/settings/update":
            return (_frame(b"hi", opcode=9), {"id": message["id"], "result": {}})
        return _cooperative(message)

    fake = await _apply_against(answer)
    assert 0xA in fake.opcodes


async def test_a_reply_split_into_fragments_with_a_ping_between_is_reassembled() -> None:
    def answer(message):
        if message.get("method") != "thread/settings/update":
            return _cooperative(message)
        body = json.dumps({"id": message["id"], "result": {"padding": "y" * 300}}).encode()
        half = len(body) // 2
        first = bytes([0x01, 126]) + struct.pack(">H", half) + body[:half]
        last = bytes([0x80, 126]) + struct.pack(">H", len(body) - half) + body[half:]
        return (first, _frame(b"", opcode=9), last)

    fake = await _apply_against(answer)
    assert 0xA in fake.opcodes


async def test_an_oversized_declared_length_is_refused_before_anything_is_read() -> None:
    started = time.monotonic()
    reason = await _refused(_on_update(bytes([0x81, 127]) + struct.pack(">Q", 1 << 40)))
    assert reason == "the Codex daemon closed the connection"
    assert time.monotonic() - started < 2.0


async def test_a_binary_frame_is_refused() -> None:
    assert await _refused(_on_update(_frame(b"{}", opcode=2))) == (
        "the Codex daemon closed the connection"
    )


async def test_a_client_payload_past_65535_bytes_uses_the_64_bit_length() -> None:
    huge = "t" * 70_000
    async with FakeDaemon(_cooperative) as daemon:
        client, _ = _client(daemon.path)
        await client.apply(huge, PermissionPreset.ASK)
    assert daemon.received[-1]["params"]["threadId"] == huge


async def test_a_daemon_request_reusing_the_id_is_not_taken_for_success() -> None:
    def replies(request_id):
        request = {"id": request_id, "method": "item/approval/request", "params": {}}
        error = {"code": -32600, "message": "refused"}
        return (request, {"id": request_id, "error": error})

    reason = await _refused(_on_update(replies))
    assert (
        reason == f"the Codex daemon refused thread/settings/update for thread {THREAD} "
        "(error -32600): Codex said “refused”"
    )


async def test_a_boolean_id_is_not_taken_for_a_reply() -> None:
    """`True == 1` in Python, so a `true` id must not answer request 1 (`initialize`)."""

    def answer(message):
        if message.get("method") == "initialize":
            refusal = {"id": True, "error": {"code": 1, "message": "x"}}
            return (refusal, {"id": message["id"], "result": {}})
        return _cooperative(message)

    await _apply_against(answer)  # succeeds: the `true` id was not taken for the reply


async def test_a_reply_holding_both_result_and_error_is_unexpected() -> None:
    def replies(request_id):
        return ({"id": request_id, "result": {}, "error": {"code": 1, "message": "x"}},)

    assert await _refused(_on_update(replies)) == "unexpected reply from the Codex daemon"


async def test_a_reply_with_neither_result_nor_error_is_unexpected() -> None:
    reason = await _refused(_on_update(lambda request_id: ({"id": request_id},)))
    assert reason == "unexpected reply from the Codex daemon"


async def test_a_reply_nested_past_the_parser_is_contained() -> None:
    nested = _frame(b"[" * 200_000 + b"]" * 200_000)
    assert await _refused(_on_update(nested)) == "unexpected reply from the Codex daemon"


async def test_a_refused_parameter_names_the_thread_and_never_a_secret() -> None:
    error = {"code": -32602, "message": POISON}
    reason = await _refused(_on_update(lambda request_id: ({"id": request_id, "error": error},)))
    assert THREAD in reason and "-32602" in reason
    assert "sk-abcdef" not in reason and "auth.json" not in reason and "/home" not in reason


async def test_a_refusal_carries_a_cleaned_excerpt_of_what_codex_said() -> None:
    """Owner, 2026-11-12 (OD-9): "Show a cleaned excerpt"."""
    said = (
        "Invalid params: unknown field `approvalsReviewr` in thread/settings/update "
        "(see /home/operator/.codex/config.toml)\nsecond line"
    )
    error = {"code": -32602, "message": said}
    reason = await _refused(_on_update(lambda request_id: ({"id": request_id, "error": error},)))
    assert "Codex said “Invalid params: unknown field `approvalsReviewr`" in reason
    for secret in ("/home", "config.toml", "operator", "second line"):
        assert secret not in reason


#: What the reviewers and the demo evaluator got past earlier cleaners (2026-11-12), and the
#: piece of each that must not come back out.
LEAKS = {
    "zero-width split token": ("bad AAAAAAAAAA\u200bAAAAAAAAAA value", "AAAAAAAAAA"),
    "zero-width split keyword": ("bad tok\u200ben=abc value", "abc"),
    "token next to non-ASCII": ("bad AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAé value", "AAAAAAAAAA"),
    "19 capitals": ("bad ABCDEFGHIJKLMNOPQRS value", "ABCDEFGHIJ"),
    "letters-only camel token": ("bad aBcDeFgHiJkLmN value", "aBcDeF"),
    "base64": ("bad abcdefghij+klmnopqrst/uvwxyz0123== value", "abcdefghij"),
    "compound key": ("bad access_token=abc value", "abc"),
    "environment key": ("OPENAI_API_KEY=abc", "abc"),
    "authorization header": ("Invalid params: Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    "json key": ('bad "token":"abc123"', "abc123"),
    "quoted value": ('password = "hunter2 correct horse"', "correct horse"),
    "bare credential word": ("invalid token abc123", "abc123"),
    "session token": ("Invalid params: session token sk-live-SECRET123", "SECRET123"),
    "secret word": ("Invalid params: secret hunter2hunter2 used", "hunter2"),
    "pairing code": ("code 4F7K-92QX expired", "4F7K"),
    "six digits": ("enter 482913 on your phone", "482913"),
    "relative path": ("bad src/remote_agents/secrets/id_rsa here", "id_rsa"),
    "home path": ("see ~alice/.ssh/id_rsa", "alice"),
    "windows path": ("at C:\\Users\\alice\\proj ok", "alice"),
    "path with a space": ("/Users/jo smith/proj/x", "smith"),
    "mac path with a space": ("/Users/owner/Library/Application Support/codex/x.db", "x.db"),
    "fullwidth keyword": ("ｔｏｋｅｎ=abc", "abc"),
    "cyrillic keyword": ("tоken=abc", "abc"),
    "ansi codes": ("\x1b[31m red\x1b[0m", "[31m"),
    "url credentials": ("https://user:pass@host/x", "user:pass"),
    "jwt": ("got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln", "eyJ"),
    "hex key": ("bad 0123456789abcdef0123456789abcdef0123 value", "0123456789"),
    "long line": ("x" * 1000, "x" * 30),
    "non-ASCII digits": ("enter \u0664\u0668\u0662\u0669\u0661 now", "\u0664\u0668"),
    "fullwidth keyword, spaced value": ("ｔｏｋｅｎ hunterpass", "hunterpass"),
    "passphrase": ("the passphrase is correct horse battery", "horse"),
    "hyphenated passphrase": ("correct-horse-battery-staple", "horse"),
    "six digits in two numbers": ("expected 482 913 here", "913"),
    "pin in two numbers": ("pin 1234 5678", "5678"),
    "card in three numbers": ("card 12345 67890 11111", "67890"),
    "lowercase pairing code": ("expected abcd-efgh got wxyz-qrst", "efgh"),
    "spelled-out letters": ("a b c d e f g h", "c d"),
    "acronym pairs": ("got ABCD EFGH", "EFGH"),
    "mnemonic": ("mnemonic abandon ability able about", "abandon"),
    "otp": ("otp 123 456", "456"),
    "split keyword": ("tok en: hunter two", "hunter"),
    "split password": ("pa ss word is hunter two", "hunter"),
    "backticked home path": ("refused for `home/alice/work`", "alice"),
    "backticked host": ("host `alice.example.com` refused", "alice"),
    "short pin": ("your pin is 482", "482"),
    "short otp": ("otp 482", "482"),
    "three non-ASCII digits": ("enter \u0664\u0668\u0662 now", "\u0664\u0668\u0662"),
    "backticked base64": ("bad `dXNlcjpwYXNz` value", "dXNlcjpwYXNz"),
    "backticked hex": ("bad `deadbeefcafebabe` value", "deadbeefcafebabe"),
}


@pytest.mark.parametrize("said, secret", LEAKS.values(), ids=LEAKS.keys())
def test_the_excerpt_never_carries_a_secret(said, secret) -> None:
    from remote_agents.adapters.agents.codex.thread_settings import _refusal

    reason = _refusal({"code": -32602, "message": said}, "thread/settings/update", "T1")
    assert secret not in reason
    assert len(reason) < 260


#: What the owner's daemon actually sends (0.167.2, 2026-11-12), and what must stay readable.
READABLE = {
    "invalid thread id": (
        "invalid thread id: invalid character: expected an optional prefix of `urn:uuid:` "
        "followed by [0-9a-fA-F-], found `n` at line 1",
        "invalid thread id: invalid character: expected an optional prefix of",
    ),
    "method not found": (
        "Method not found: thread/settings/update",
        "Method not found: thread/settings/update",
    ),
    "unknown field": (
        "Invalid params: unknown field `approvalsReviewr`, expected one of `threadId`, `model`",
        "unknown field `approvalsReviewr`, expected one of `threadId`, `model`",
    ),
    "unknown method": (
        "Invalid request: unknown variant `thread/settings/update`, expected one of "
        "`initialize`, `thread/start`",
        "unknown variant `thread/settings/update`",
    ),
}


@pytest.mark.parametrize("said, kept", READABLE.values(), ids=READABLE.keys())
def test_the_excerpt_keeps_what_codex_actually_says(said, kept) -> None:
    from remote_agents.adapters.agents.codex.thread_settings import _refusal

    assert kept in _refusal({"code": -32600, "message": said}, "thread/settings/update", "T1")


def test_an_unknown_method_names_the_thread_code_and_excerpt() -> None:
    from remote_agents.adapters.agents.codex.thread_settings import _refusal

    said = READABLE["unknown method"][0]
    reason = _refusal({"code": -32600, "message": said}, "thread/settings/update", "T1")
    assert reason.startswith("this Codex does not offer thread/settings/update for thread T1")
    assert "(error -32600): Codex said “Invalid request: unknown variant" in reason


def test_a_credential_word_drops_the_whole_excerpt() -> None:
    from remote_agents.adapters.agents.codex.thread_settings import _refusal

    reason = _refusal({"code": 1, "message": "token expired"}, "thread/settings/update", "T1")
    assert reason == "the Codex daemon refused thread/settings/update for thread T1 (error 1)"


def _raw(first: int, second: int, payload: bytes = b"") -> bytes:
    return bytes([first, second]) + payload


@pytest.mark.parametrize(
    "frames",
    [
        pytest.param((_raw(0x80, 2, b"{}"),), id="continuation-with-nothing-to-continue"),
        pytest.param((_raw(0x01, 2, b'{"'), _raw(0x81, 2, b"{}")), id="text-frame-mid-message"),
        pytest.param((_raw(0xC1, 2, b"{}"),), id="reserved-bit-set"),
        pytest.param((_raw(0x89, 126) + struct.pack(">H", 200) + b"p" * 200,), id="oversized-ping"),
        pytest.param((_raw(0x09, 1, b"p"),), id="fragmented-ping"),
        pytest.param((_raw(0x83, 2, b"{}"),), id="reserved-data-opcode"),
    ],
)
async def test_every_framing_violation_ends_the_connection(frames) -> None:
    assert await _refused(_on_update(*frames)) == "the Codex daemon closed the connection"


async def test_a_socket_owned_by_another_user_is_never_spoken_to(monkeypatch) -> None:
    import os

    async with FakeDaemon(_cooperative) as daemon:
        monkeypatch.setattr(os, "getuid", lambda: os.stat(daemon.path).st_uid + 1)
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.apply(THREAD, PermissionPreset.ASK)
    assert raised.value.reason == "the Codex daemon socket is not this user's"
    assert daemon.connections == 0


async def test_a_repeated_cursor_ends_the_paging() -> None:
    def answer(message):
        if message.get("method") != "thread/loaded/list":
            return _cooperative(message)
        return ({"id": message["id"], "result": {"data": ["a"], "nextCursor": "same"}},)

    async with FakeDaemon(answer) as daemon:
        client, _ = _client(daemon.path)
        with pytest.raises(ThreadPermissionsError) as raised:
            await client.loaded_threads()
    assert raised.value.reason == "unexpected reply from the Codex daemon"
    assert daemon.connections == 2
