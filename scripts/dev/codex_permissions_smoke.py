"""Smoke the Codex thread-permissions client against this host's real shared daemon.

Opens one plain `codex` pane in a fresh temp folder on a private tmux socket, finds its thread on
the daemon, applies each explicit preset through `CodexThreadPermissions`, and reads the pane's
status line for the label Codex draws for it. Sends no prompt, so it spends no quota. Exits the
pane with `/exit` and ends only its own tmux session; it never stops or restarts the daemon,
which a plain `codex` starts if none is running (the state Remote Control wants anyway).

    uv run --locked python -I scripts/dev/codex_permissions_smoke.py

A folder-trust question stops the smoke instead of being answered: answering writes the owner's
Codex config. The update offer is skipped and new hooks are left untrusted, for the same reason.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from remote_agents.adapters.agents.codex import thread_settings
from remote_agents.adapters.agents.codex.thread_settings import CodexThreadPermissions
from remote_agents.domain.profiles import PermissionPreset

SOCKET = "ra-perm-smoke"
SESSION = "smoke"
#: What Codex 0.167 draws in its status line for each preset (measured 2026-11-12).
LABELS = {
    PermissionPreset.APPROVE_FOR_ME: "Workspace · Approve for me",
    PermissionPreset.ASK: "Workspace · Ask for approval",
    PermissionPreset.FULL_ACCESS: "Full Access",
}


class SmokeFailed(Exception):
    pass


def tmux(*arguments: str) -> str:
    completed = subprocess.run(
        ["tmux", "-L", SOCKET, *arguments], capture_output=True, text=True, check=False
    )
    return completed.stdout


def screen() -> str:
    return tmux("capture-pane", "-p", "-t", SESSION)


async def loaded_threads() -> set[str]:
    """`thread/loaded/list` on the daemon, over the same transport the client uses."""
    client = CodexThreadPermissions()
    path = await client._socket_path()
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        await thread_settings._handshake(reader, writer)
        await thread_settings._send(
            writer,
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "ra-smoke", "version": "1"}},
            },
        )
        while (await thread_settings._message(reader, writer)).get("id") != 1:
            pass
        await thread_settings._send(writer, {"method": "initialized"})
        await thread_settings._send(writer, {"id": 2, "method": "thread/loaded/list", "params": {}})
        while True:
            message = await thread_settings._message(reader, writer)
            if message.get("id") == 2:
                return set(message.get("result", {}).get("data", []))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def open_pane(folder: Path) -> None:
    tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", str(folder), "codex")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        text = screen()
        if "Trust this folder?" in text:
            raise SmokeFailed("Codex asked about folder trust; answering would write its config")
        if "Skip until next version" in text:
            tmux("send-keys", "-t", SESSION, "2")
        elif "Continue without trusting" in text:
            tmux("send-keys", "-t", SESSION, "3")
        elif "Ready" in text and "? for shortcuts" in text:
            return
        time.sleep(1)
    raise SmokeFailed("the Codex pane never reached its composer:\n" + screen())


async def smoke() -> None:
    if shutil.which("codex") is None:
        raise SmokeFailed("codex is not installed")
    before = await loaded_threads() if await _daemon_up() else set()
    folder = Path(tempfile.mkdtemp(prefix="ra-perm-smoke-"))
    try:
        open_pane(folder)
        after = await loaded_threads()
        new = after - before
        if len(new) != 1:
            raise SmokeFailed(f"expected one new thread on the daemon, found {len(new)}")
        (thread,) = new
        client = CodexThreadPermissions()
        for preset, label in LABELS.items():
            await client.apply(thread, preset)
            deadline = time.monotonic() + 10
            while label not in screen():
                if time.monotonic() > deadline:
                    raise SmokeFailed(f"{preset}: status line never showed {label!r}:\n{screen()}")
                await asyncio.sleep(0.5)
            print(f"ok {preset.value}")
    finally:
        tmux("send-keys", "-t", SESSION, "/exit", "Enter")
        time.sleep(1)
        tmux("send-keys", "-t", SESSION, "Enter")
        time.sleep(1)
        tmux("kill-session", "-t", SESSION)
        shutil.rmtree(folder, ignore_errors=True)


async def _daemon_up() -> bool:
    try:
        await CodexThreadPermissions()._socket_path()
    except thread_settings.ThreadPermissionsError:
        return False
    return True


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    try:
        asyncio.run(smoke())
    except (SmokeFailed, thread_settings.ThreadPermissionsError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
