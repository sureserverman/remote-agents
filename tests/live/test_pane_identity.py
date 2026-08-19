"""Opt-in proof, on real tmux, that a launched agent's identity travels with its pane.

Stage 1's whole claim is that identity stops being a property of the session's window and
becomes a property of the pane. Three things have to be true on the host's own tmux for
that claim to hold, and all three are checked here against a disposable socket rather than
inferred from the codec's tests, which only ever see hand-built strings:

1. A real `gateway.launch` stamps every identity field on the pane.
2. The stamp survives a real `swap-pane` into a foreign session — the exact move the
   console will make — and inventory still reports one managed session, now hosted
   elsewhere.
3. The home session the pane left behind carries **no** identity for the arriving pane to
   inherit. This is the one that would fail silently: tmux resolves `#{@option}` by falling
   back pane -> session, so a session-scoped twin would make the pane that swapped in
   answer with the agent's identity — and, once the agent died, answer as though it were
   still alive.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_IDENTITY_OPTIONS = (
    "@remote_agents_schema",
    "@remote_agents_id",
    "@remote_agents_project_id",
    "@remote_agents_profile",
)


def write_intent(directory: Path, session_id: SessionId, cwd: Path) -> None:
    """Give `session_runner` something long-lived to exec into.

    The launch path is the thing under test, so it is driven for real rather than
    fabricated — and a real launch execs whatever its intent names. Without an intent the
    runner exits immediately and `remain-on-exit` leaves a corpse: `pane_dead=1` before the
    swap even happens, so what would be exchanged is a dead pane rather than a running
    agent, and the test would be measuring its own fixture. `sleep` stands in for the
    agent; no curated profile is needed to prove that a pane keeps its identity.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    sleep = "/bin/sleep" if Path("/bin/sleep").exists() else "/usr/bin/sleep"
    (directory / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": str(session_id),
                "profile_id": "claude",
                "executable": sleep,
                "argv": [sleep, "600"],
                "cwd": str(cwd),
                "environment": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            }
        ),
        encoding="utf-8",
    )


async def test_a_launched_pane_carries_its_identity_wherever_it_is_hosted(
    tmp_path: Path,
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    session_id = SessionId.new()
    socket = f"remote-agents-test-{session_id.value.hex}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner, intent_directory=tmp_path / "intents")
    base = ("tmux", "-L", socket)
    write_intent(tmp_path / "intents", session_id, tmp_path)
    try:
        await gateway.launch(session_id, ProjectId("qualification"), ProfileId("claude"), tmp_path)

        # (1) The launch stamped the pane itself.
        agent_pane = (
            await runner.run(*base, "list-panes", "-t", f"ra-{session_id}:", "-F", "#{pane_id}")
        ).strip()
        for option in _IDENTITY_OPTIONS:
            value = await runner.run(
                *base, "display-message", "-p", "-t", agent_pane, f"#{{{option}}}"
            )
            assert value.strip(), f"{option} is not set on the launched pane"

        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]
        assert inventory.managed[0].pane_id == agent_pane
        assert inventory.managed[0].session_name == f"ra-{session_id}"

        # (2) The exact exchange the console will perform, against a foreign session.
        await gateway.create_console(("sleep", "600"), tmp_path)
        console_pane = (
            await runner.run(*base, "list-panes", "-t", "ra-console:", "-F", "#{pane_id}")
        ).strip()
        await runner.run(*base, "swap-pane", "-s", agent_pane, "-t", console_pane)

        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]
        displaced = inventory.managed[0]
        assert displaced.pane_id == agent_pane, "the pane id is the identity that travelled"
        assert displaced.session_name == "ra-console", "and it is now hosted by the console"
        assert displaced.project_id == ProjectId("qualification")
        assert displaced.profile_id == ProfileId("claude")

        # (3) The home session is bare: the arriving pane inherits no identity from it.
        for option in _IDENTITY_OPTIONS:
            value = await runner.run(
                *base, "display-message", "-p", "-t", console_pane, f"#{{{option}}}"
            )
            assert value.strip() == "", (
                f"{option} leaked to the pane that swapped into the home window"
            )

        # Both sessions survived the exchange — this is why the design is swap-based.
        sessions = set(
            (await runner.run(*base, "list-sessions", "-F", "#{session_name}")).splitlines()
        )
        assert {f"ra-{session_id}", "ra-console"} <= sessions
    finally:
        try:
            await runner.run(*base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)
