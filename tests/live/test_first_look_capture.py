"""BL-137's measurement (Task 3.1 of the backlog-sweep plan's Sub-plan 3): what a resumed Codex
pane shows each time the restart pass comes to type into it, with nothing ever typed.

The composition, the isolated home and the helpers are `test_restart_resume_live`'s; the drill
answers each send itself (`_Process.capture_only`). Results:
`docs/acceptance-2026-11-surviving-codex-turn.md`. Opt-in twice -- `REMOTE_AGENTS_LIVE_ACCEPTANCE=1`
and `REMOTE_AGENTS_FIRST_LOOK_CAPTURE=1` -- since every run spends one short Codex turn, and kept
apart from the live gate's drills so they run with no skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from codex_home import retire_codex_home, short_codex_home

from live.test_restart_resume_live import (
    _LONG_TURN,
    _MCP_DELAY,
    _PASS_SECONDS,
    _POLL_SECONDS,
    CODEX,
    _alive,
    _answer_trust,
    _diagnosis,
    _isolated_home,
    _key,
    _prerequisites,
    _Process,
    _stop_codex_daemon,
    _type,
    _until_gone,
    _until_running,
    _until_sleeping,
    _workspace,
)
from remote_agents.adapters.agents.turn_markers import FileTurnMarkers
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.commands import LaunchCommand
from remote_agents.composition.service import _serve_with_reconciliation
from remote_agents.config import TelegramSecrets, load_config
from remote_agents.domain.models import SessionState
from remote_agents.domain.profiles import PermissionPreset


@pytest.mark.live_acceptance
@pytest.mark.parametrize("shape", ["survivor", "boot"])
async def test_first_look_capture(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-137's measurement (Task 3.1): what a resumed Codex pane shows each time the restart
    pass comes to type into it, with nothing ever typed.

    `survivor`: only the tmux server is killed under a turn running `sleep 40`; the daemon and
    the turn live on. `boot`: a reboot -- the server and the daemon go -- under the same turn,
    with an MCP server that keeps Codex's boot waiting `_MCP_DELAY` seconds. Each look is
    printed as one `FIRST_LOOK` JSON line. Opt-in twice: it spends one short turn per run
    (OD-2), and asserts only that the pass looked."""
    if os.environ.get("REMOTE_AGENTS_FIRST_LOOK_CAPTURE") != "1":
        pytest.skip("BLOCKED: not asked for; run with REMOTE_AGENTS_FIRST_LOOK_CAPTURE=1")
    claude, codex = _prerequisites()
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-rfl-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
    codex_home = short_codex_home()
    repo: Path | None = None
    connections = []
    seen: dict[str, object] = {"shape": shape, "codex": _codex_version(codex)}
    try:
        repo = _workspace(tmp_path)
        paths = _isolated_home(
            tmp_path / "home",
            repo,
            claude,
            codex,
            codex_home,
            mcp_delay=_MCP_DELAY if shape == "boot" else None,
        )
        config = load_config(paths.config_path)
        connections.append(open_database(paths.database_path, migrations=MIGRATIONS))
        first = _Process(config, connections[-1], paths)
        outcome = await first.backend.sessions.launch(
            LaunchCommand(
                first.project(repo),
                CODEX,
                _key(f"first-look-{shape}"),
                "restart-live",
                # Shares the daemon, so a tmux-only crash leaves the turn running there.
                permissions=PermissionPreset.PROVIDER_DEFAULT,
            )
        )
        session_id = outcome.record.session_id
        if outcome.record.state is SessionState.UNTRUSTED:
            await _answer_trust(first, session_id)
        await _until_running(first, session_id, socket)
        await _type(first, session_id, _LONG_TURN)
        markers = FileTurnMarkers(paths.activity_directory)
        await _until_sleeping(first, markers, session_id)

        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=30)
        if shape == "boot":
            _stop_codex_daemon(codex, codex_home)
        await _until_gone(first)
        seen["after_kill"] = _alive(codex_home)
        killed = time.monotonic()

        connections.append(open_database(paths.database_path, migrations=MIGRATIONS))
        second = _Process(config, connections[-1], paths)
        second.capture_only = True

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            deadline = time.monotonic() + _PASS_SECONDS
            while not second.notices and time.monotonic() < deadline:
                await asyncio.sleep(_POLL_SECONDS)

        await _serve_with_reconciliation(
            TelegramSecrets("token", 7, 11), second.composition(), poll, 3600
        )
        seen["after_pass"] = _alive(codex_home)
        seen["notices"] = [
            [(str(report.record.session_id), report.outcome) for report in reports]
            for reports, _ in second.notices
        ]
        looks = [look for session_looks in second.looks.values() for look in session_looks]
        for index, look in enumerate(looks):
            print(
                "FIRST_LOOK",
                json.dumps({**seen, "look": index, "after_kill_s": look["at"] - killed, **look}),
            )
        assert looks, await _diagnosis(second, f"the pass never came to type: {seen}")
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["tmux", "-S", str(socket), "kill-server"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        retire_codex_home(codex_home)
        for connection in connections:
            with contextlib.suppress(Exception):
                connection.close()
        if repo is not None:
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(tmux_directory, ignore_errors=True)


def _codex_version(codex: Path) -> str:
    completed = subprocess.run(
        [str(codex), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return completed.stdout.strip()
