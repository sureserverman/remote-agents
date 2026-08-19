"""Opt-in composer journey against real tmux: tabs live, sessions never depend on them.

Stage 1's live file proves the raw window operations; this one proves the *composer's*
journey over them on a disposable socket: ensure creates the console running a dashboard
command, sync links a live session's tab and reconciles it away when the session ends,
open focuses the tab (select-window is headless-safe — no attached client exists in CI,
so the switch-client fallback is exactly the branch that must NOT be taken here), and
killing the console leaves the managed session observable — DEC-006 proven one layer up
from where Stage 1 proved it.

The managed session is fabricated (options set by hand) rather than launched through an
agent profile: what is under test is window composition, and real launches carry their
own live proofs elsewhere in this directory.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.application.console import ConsoleComposer
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def _record(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("qualification"),
        ProfileId("claude"),
        SessionDisplayIdentity("qualification", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


async def test_the_composer_journey_holds_on_real_tmux(tmp_path: Path) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    session_id = SessionId.new()
    socket = f"remote-agents-test-{session_id.value.hex}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner)
    composer = ConsoleComposer(gateway, ("sleep", "600"), tmp_path)
    base = ("tmux", "-L", socket)
    try:
        assert await composer.ensure() is True
        assert await gateway.console_exists() is True
        # ensure is idempotent against a console that already exists
        assert await composer.ensure() is True

        name = f"ra-{session_id}"
        await runner.run(*base, "new-session", "-d", "-s", name, "sleep", "600")
        for option, value in (
            ("@remote_agents_schema", "1"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "claude"),
        ):
            await runner.run(*base, "set-option", "-t", f"{name}:", option, value)

        await composer.sync((_record(session_id, SessionState.RUNNING),))
        windows = dict(await gateway.console_windows())
        assert session_id in windows.values(), "a live session's tab must exist after sync"

        # open() must take the tab route: select-window works headless, and the
        # switch-client fallback would fail here with "no current client" — so reaching
        # the end of open() without an exception IS the proof the tab route was taken.
        await composer.open(session_id)

        # jump home is the same select, aimed at the dashboard
        await gateway.select_console_window(0)

        await composer.sync((_record(session_id, SessionState.ENDED),))
        assert session_id not in dict(await gateway.console_windows()).values()

        await composer.sync((_record(session_id, SessionState.RUNNING),))
        await runner.run(*base, "kill-session", "-t", "ra-console")
        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]
    finally:
        try:
            await runner.run(*base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)
