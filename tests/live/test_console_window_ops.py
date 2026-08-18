"""Opt-in proof, on real tmux, of the two assumptions the console model stands on.

Task 1.3 dedupes and drops console-view lines because `list-panes -a` re-reports every
linked window under the console's name with its schema options blanked. That claim was
established by hand against tmux 3.4 on 2026-08-18; this test keeps it established, on the
same disposable-socket shape every other live file uses, so a tmux upgrade that changes
linked-window listing or option inheritance fails here instead of corrupting inventory in
production. The second assumption is DEC-006's: nothing about linking a window into the
console makes the managed session depend on the console — unlinking, and even killing the
console outright, leaves the session observable and alive.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.domain.models import SessionId


async def test_console_linking_never_pollutes_inventory_or_owns_a_session(
    tmp_path: Path,
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    session_id = SessionId.new()
    socket = f"remote-agents-test-{session_id.value.hex}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner)
    base = ("tmux", "-L", socket)
    try:
        await gateway.create_console(("sleep", "600"), tmp_path)
        assert await gateway.console_exists() is True

        # A managed-shaped session, fabricated directly: what is under test here is the
        # window/inventory behavior, not the launch path, which has its own live proof.
        name = f"ra-{session_id}"
        await runner.run(*base, "new-session", "-d", "-s", name, "sleep", "600")
        for option, value in (
            ("@remote_agents_schema", "1"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "claude"),
        ):
            await runner.run(*base, "set-option", "-t", f"{name}:", option, value)

        await gateway.link_session_window(session_id)

        # The duplication assumption, live: the linked window is listed again under the
        # console, and inventory must still see exactly one clean observation.
        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]
        assert inventory.orphans == ()

        windows = dict(await gateway.console_windows())
        linked = [index for index, owner in windows.items() if owner == session_id]
        assert len(linked) == 1 and linked[0] >= 1

        await gateway.unlink_console_window(linked[0])
        assert session_id not in dict(await gateway.console_windows()).values()
        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]

        # DEC-006, live: the console dying is a presentation event, not a lifecycle one.
        await gateway.link_session_window(session_id)
        await runner.run(*base, "kill-session", "-t", "ra-console")
        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]
        assert await gateway.console_exists() is False
    finally:
        try:
            await runner.run(*base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)
