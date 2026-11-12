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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from remote_agents.adapters.agents.codex.thread_settings import CodexThreadPermissions
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.thread_permissions import ThreadPermissionsError

SOCKET = "ra-perm-smoke"
SESSION = "smoke"
#: What Codex 0.167 draws in its status line for each preset (measured 2026-11-12). `ask` comes
#: first and each label must be absent before its apply, so a label already on screen -- the
#: owner's own default, say -- can never pass for a change.
LABELS = {
    PermissionPreset.ASK: "Workspace · Ask for approval",
    PermissionPreset.FULL_ACCESS: "Full Access",
    PermissionPreset.APPROVE_FOR_ME: "Workspace · Approve for me",
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


def open_pane(folder: Path) -> None:
    """Start `codex` and walk it to its composer, or raise before a single key reaches a dialog
    whose answer would write anything (folder trust above all)."""
    tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", str(folder), "codex")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        text = screen()
        if "Trust this folder?" in text:
            raise SmokeFailed("Codex asked about folder trust; answering would write its config")
        if "Skip until next version" in text:
            tmux("send-keys", "-t", SESSION, "2")  # "Skip": this run only
        elif "Continue without trusting" in text:
            tmux("send-keys", "-t", SESSION, "3")  # new hooks stay untrusted
        elif "Ready" in text and "? for shortcuts" in text:
            return
        time.sleep(1)
    raise SmokeFailed("the Codex pane never reached its composer:\n" + screen())


async def smoke() -> None:
    if shutil.which("codex") is None or shutil.which("tmux") is None:
        raise SmokeFailed("codex and tmux must both be installed")
    client = CodexThreadPermissions()
    before = await _loaded(client)
    folder = Path(tempfile.mkdtemp(prefix="ra-perm-smoke-")).resolve()
    at_composer = False
    try:
        open_pane(folder)
        at_composer = True
        thread = await _own_thread(client, before, folder)
        for preset, label in LABELS.items():
            if label in screen():
                raise SmokeFailed(f"{preset}: {label!r} was on screen before the apply")
            await client.apply(thread, preset)
            deadline = time.monotonic() + 10
            while label not in screen():
                if time.monotonic() > deadline:
                    raise SmokeFailed(f"{preset}: status line never showed {label!r}:\n{screen()}")
                await asyncio.sleep(0.5)
            print(f"ok {preset.value}")
    finally:
        if at_composer:
            # Keys only into a pane known to sit on its composer: before that, a dialog may be
            # up, and `Enter` would answer it.
            tmux("send-keys", "-t", SESSION, "/exit", "Enter")
            time.sleep(1)
            tmux("send-keys", "-t", SESSION, "Enter")
            time.sleep(1)
        tmux("kill-session", "-t", SESSION)
        shutil.rmtree(folder, ignore_errors=True)


async def _loaded(client: CodexThreadPermissions) -> frozenset[str]:
    try:
        return await client.loaded_threads()
    except ThreadPermissionsError as error:
        if error.reason == "no Codex daemon":
            return frozenset()  # the pane's own `codex` starts it
        raise


async def _own_thread(client: CodexThreadPermissions, before: frozenset[str], folder: Path) -> str:
    """The one new thread whose recorded folder is this smoke's own -- never another session's."""
    new = (await client.loaded_threads()) - before
    own = [thread for thread in new if (await client.thread_folder(thread)) == folder]
    if len(own) != 1:
        raise SmokeFailed(f"expected one new thread in {folder}, found {len(own)}")
    return own[0]


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    try:
        asyncio.run(smoke())
    except (SmokeFailed, ThreadPermissionsError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
