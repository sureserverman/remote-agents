"""A workflow rollover on a real tmux server: one `ready` becomes one successor, typed the
adoption template, and the predecessor is stopped only once that successor accepted.

A harmless fake agent draws a one-line composer (a rule, then `❯ `) and echoes each line as
`GOT: <line>`. Told `handoff`, it writes a `ready` envelope naming its own managed session, as
the planning plugin's executor does at a gate; shown the adoption template, it writes
`accepted` -- or `failed`, when the test says so -- naming its own. The pass is the one `serve`
builds (`build_rollover_pass`), over the real SQLite stores, the real `SessionService`, the real
`TmuxTerminal` and the real envelope reader, so the launch, the guarded send, the idle check and
the graceful stop are all the production ones. Run alone: real-tmux tests fail spuriously
beside another uv/pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE
from remote_agents.ports.terminal import PromptOutcome

PROJECT = ProjectId("opaque-editor")
PROFILE = ProfileId("fake")
HANDOFF = "h-0123456789abcdef0123"
TEMPLATE = f"/planning:executing-plans --adopt-handoff {HANDOFF}"

_AGENT = """
import json, os, sys, time
from pathlib import Path

HANDOFF, MODE = sys.argv[1], Path(sys.argv[2])
me = os.environ["REMOTE_AGENTS_SESSION_ID"]
handoffs = Path.cwd() / ".claude" / "handoffs"


def envelope(event, suffix, **extra):
    handoffs.mkdir(parents=True, exist_ok=True)
    body = {
        "protocol": "remote-agents-handoff",
        "version": 1,
        "event": event,
        "handoff_id": HANDOFF,
        "managed_session_id": me,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "plan": "/work/plan.md",
        **extra,
    }
    (handoffs / f"{HANDOFF}.{suffix}.json").write_text(json.dumps(body))


print("READY", flush=True)
while True:
    print("─" * 24, flush=True)
    try:
        line = input("❯ ")
    except (EOFError, KeyboardInterrupt):
        break
    print("GOT: " + line, flush=True)
    if line == "handoff":
        envelope("HANDOFF_READY", "ready")
    elif line == "/planning:executing-plans --adopt-handoff " + HANDOFF:
        if MODE.read_text() == "accept":
            envelope("HANDOFF_ACCEPTED", "accepted")
        else:
            envelope("HANDOFF_FAILED", "failed", failure_code="branch-mismatch")
"""

#: The fake draws no command menu at all, as Claude draws none once arguments follow a `/`
#: command (2026-10-05 live run). So its `command_menu` never matches, and `menu_absent` -- the
#: composer's rule with nothing drawn above it that is a menu -- says so: the adoption template
#: goes in by the same rule real Claude's does. (It once read the command off the draft line,
#: a stand-in for a menu that agrees, which no real screen shows; that is what let the real
#: refusal through to the live run.)
_COMPOSER = ComposerScreen(
    composer=r"^─{10,}\n❯ ?(?P<draft>[^\n]*)\Z",
    command_menu=r"(?!)",
    menu_absent=r"─{10,}\n❯",
)


async def _type(terminal: TmuxTerminal, session_id: SessionId, text: str) -> None:
    """The test's own cue, through the guarded send, tried while the agent draws its composer."""
    delivery = None
    for _ in range(50):
        delivery = await terminal.send_prompt(session_id, text)
        if delivery.outcome is PromptOutcome.SENT:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"the cue was not typed: {delivery}")


@pytest.mark.parametrize("mode", ["accept", "fail"])
async def test_a_ready_rolls_over_to_one_successor_and_stops_only_on_accept(
    tmp_path: Path, mode: str
) -> None:
    project = tmp_path / "opaque-editor"
    (project / ".git").mkdir(parents=True)
    agent, mode_file = tmp_path / "fake_agent.py", tmp_path / "mode"
    agent.write_text(_AGENT, encoding="utf-8")
    mode_file.write_text(mode, encoding="utf-8")
    socket = f"remote-agents-test-{uuid4().hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "i")
    terminal = TmuxTerminal(
        gateway,
        {PROJECT: project},
        {},
        startup_timeout=10.0,
        # Per session, as production builds every profile: the session is named in its own
        # environment, which is how the agent knows whom its envelopes speak for.
        profile_factories={
            PROFILE: lambda session_id, _remote_control: LaunchProfile(
                sys.executable,
                (sys.executable, str(agent), HANDOFF, str(mode_file)),
                {"PATH": os.environ["PATH"], SESSION_ID_VARIABLE: str(session_id)},
                "READY",
            )
        },
        composers={"fake": ProviderDescriptor(PROFILE, "F", composer=_COMPOSER)},
    )
    connection = open_database(tmp_path / "sessions.sqlite3")
    sessions = SessionService(SQLiteSessionStore(connection), terminal)

    async def on() -> bool:
        return True

    rollover_pass = build_rollover_pass(
        connection=connection,
        sessions=sessions,
        terminal=terminal,
        enabled=on,
        project_paths={PROJECT: project},
        rollable=frozenset({PROFILE}),
    )
    rollovers = SQLiteRolloverStore(connection)
    launched: list[SessionId] = []
    try:
        predecessor = (await sessions.launch(LaunchCommand(PROJECT, PROFILE, "pred"))).record
        launched.append(predecessor.session_id)
        await _type(terminal, predecessor.session_id, "handoff")

        want = RolloverState.COMPLETED if mode == "accept" else RolloverState.FAILED
        row = None
        for _ in range(60):
            await rollover_pass.run_once()
            launched.extend(
                record.session_id
                for record in await sessions.list_sessions()
                if record.session_id not in launched
            )
            rows = rollovers._select("1 = 1", ())
            row = rows[0] if rows else None
            if row is not None and row.state is want:
                break
            await asyncio.sleep(0.2)

        assert row is not None and row.state is want, (
            row.state,
            row.failure_code,
            row.failure_detail,
        )
        assert len(launched) == 2, "exactly one successor"
        successor = row.successor_session_id
        assert successor is not None and successor != predecessor.session_id
        assert TEMPLATE in await terminal.capture(successor), "the template was typed into it"
        managed = {pane.session_id for pane in (await gateway.inventory()).managed}
        states = {record.session_id: record.state for record in await sessions.list_sessions()}
        if mode == "accept":
            assert predecessor.session_id not in managed, "the predecessor pane is gone"
            assert states[predecessor.session_id] is SessionState.ENDED
            assert await rollovers.continued_as(predecessor.session_id) == successor
        else:
            assert row.failure_code == "branch-mismatch"
            assert predecessor.session_id in managed, "the predecessor pane is still alive"
            assert states[predecessor.session_id] is SessionState.RUNNING
            assert await rollovers.continued_as(predecessor.session_id) is None
        assert successor in managed
        # Finished envelopes are removed, so the directory does not fill.
        assert not list((project / ".claude" / "handoffs").glob(f"{HANDOFF}.*.json"))
    finally:
        for session_id in launched:
            try:
                await gateway.destroy(session_id)
            except RuntimeError:
                pass
        killing = await asyncio.create_subprocess_exec(
            "tmux", "-L", socket, "kill-server", stderr=asyncio.subprocess.DEVNULL
        )
        await killing.wait()
        connection.close()
