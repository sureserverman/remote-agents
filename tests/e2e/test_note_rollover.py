"""Rollover with a note on a real tmux server, with fake agents (BL-119, Sub-plan 5 Task 2.5).

A harmless fake agent draws a one-line composer (a rule, then `❯ `) and echoes each line as
`GOT: <line>`. Asked by the service's fixed request to write a handoff note at a path, it
writes one there -- unless the test made it one that ignores the request. Typed the fixed first
message naming a note, it prints the note's fact and reports a finished turn the way the hook
drain records one (a `reported` `completed` row in the store) -- unless the test made it a
successor that never ends a turn.

The pass is the one `serve` builds (`build_rollover_pass`) with the auto-rollover switch off,
over the real SQLite stores, the real `SessionService`, the real `TmuxTerminal`, the real note
reader and the real activity store, so the guarded send, the idle check, the launch and the
graceful stop are the production ones. Only the patiences are shortened. Run alone: real-tmux
tests fail spuriously beside another uv/pytest.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application import rollover as rollover_module
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.rollover import RolloverKind, RolloverState
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

PROJECT = ProjectId("plainless")
PROFILE = ProfileId("fake")
FACT = "the-fact-is-mauve-42"

_AGENT = """
import os, re, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

MODE, DATABASE, FACT = Path(sys.argv[1]).read_text(), sys.argv[2], sys.argv[3]
me = os.environ["REMOTE_AGENTS_SESSION_ID"]
ASK = re.compile(r"Write a handoff note for a fresh session to `([^`]+)`")
READ = re.compile(r"Read `([^`]+)` and continue the work it describes")

print("READY", flush=True)
while True:
    print("─" * 24, flush=True)
    try:
        line = input("❯ ")
    except (EOFError, KeyboardInterrupt):
        break
    print("GOT: " + line, flush=True)
    asked = ASK.search(line)
    if asked and MODE != "ignore":
        Path(asked.group(1)).write_text("# Handoff\\n" + FACT + "\\n")
    told = READ.search(line)
    if told:
        print("NOTE SAYS: " + Path(told.group(1)).read_text().splitlines()[1], flush=True)
        if MODE != "silent":
            # What the hook drain records at a turn's end (DEC-037), written as it writes it.
            with sqlite3.connect(DATABASE) as store:
                store.execute(
                    "INSERT INTO agent_activity(session_id, kind, detail, confidence, observed_at)"
                    " VALUES (?, 'completed', 'read the note', 'reported', ?)",
                    (me, datetime.now(timezone.utc).isoformat()),
                )
"""

#: The fake's composer, allowing a draft the pane wrapped over several lines: the note request
#: is longer than a pane is wide, as it is in a real agent's composer.
_COMPOSER = ComposerScreen(
    composer=r"(?s)^─{10,}\n❯ ?(?P<draft>[^─]*)\Z",
    command_menu=r"(?!)",
    menu_absent=r"─{10,}\n❯",
)


def _git_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "--template=", "--initial-branch=main", str(root)], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "Fixture",
        ],
        check=True,
    )


@pytest.fixture
def short_patiences(monkeypatch):
    monkeypatch.setattr(rollover_module, "NOTE_PATIENCE", timedelta(seconds=4))
    monkeypatch.setattr(rollover_module, "ADOPTION_PATIENCE", timedelta(seconds=5))


@pytest.mark.parametrize("mode", ["write", "ignore", "silent"])
async def test_a_note_rollover_on_real_tmux(tmp_path: Path, mode: str, short_patiences) -> None:
    project = tmp_path / "plainless"
    _git_fixture(project)
    agent, mode_file = tmp_path / "fake_agent.py", tmp_path / "mode"
    agent.write_text(_AGENT, encoding="utf-8")
    mode_file.write_text(mode, encoding="utf-8")
    database = tmp_path / "sessions.sqlite3"
    socket = f"remote-agents-test-{uuid4().hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "i")
    terminal = TmuxTerminal(
        gateway,
        {PROJECT: project},
        {},
        startup_timeout=10.0,
        profile_factories={
            PROFILE: lambda session_id, _remote_control: LaunchProfile(
                sys.executable,
                (sys.executable, str(agent), str(mode_file), str(database), FACT),
                {"PATH": os.environ["PATH"], SESSION_ID_VARIABLE: str(session_id)},
                "READY",
            )
        },
        composers={"fake": ProviderDescriptor(PROFILE, "F", "Fake", composer=_COMPOSER)},
    )
    connection = open_database(database)
    sessions = SessionService(SQLiteSessionStore(connection), terminal)

    async def off() -> bool:
        return False  # the press is the consent; the switch is not read for a note rollover

    rollover_pass = build_rollover_pass(
        connection=connection,
        sessions=sessions,
        terminal=terminal,
        enabled=off,
        project_paths={PROJECT: project},
        adopt_templates={PROFILE: adopt_templates()[ProfileId("claude")]},
        note_rollable=frozenset({PROFILE}),
    )
    rollovers = SQLiteRolloverStore(connection)
    launched: list[SessionId] = []
    try:
        predecessor = (await sessions.launch(LaunchCommand(PROJECT, PROFILE, "pred"))).record
        launched.append(predecessor.session_id)
        asked = await rollovers.request(
            predecessor.session_id,
            project_id=PROJECT,
            profile_id=PROFILE,
            at=predecessor.created_at,
            kind=RolloverKind.NOTE,
        )
        assert asked is not None
        note = project / ".claude" / "handoffs" / f"{asked.id}.note.md"

        want = {
            "write": RolloverState.COMPLETED,
            "ignore": RolloverState.FAILED,
            "silent": RolloverState.FAILED,
        }[mode]
        row = None
        for _ in range(120):
            await rollover_pass.run_once()
            launched.extend(
                record.session_id
                for record in await sessions.list_sessions()
                if record.session_id not in launched
            )
            row = await rollovers.get(asked.id)
            if row is not None and row.state is want:
                break
            await asyncio.sleep(0.2)

        assert row is not None and row.state is want, (
            row.state,
            row.failure_code,
            row.failure_detail,
        )
        if mode != "write":  # a completed rollover's predecessor pane is gone
            predecessor_screen = await terminal.capture(predecessor.session_id)
            assert "GOT: Write a handoff note for a fresh session to" in predecessor_screen
        managed = {pane.session_id for pane in (await gateway.inventory()).managed}
        states = {record.session_id: record.state for record in await sessions.list_sessions()}
        if mode == "ignore":
            assert row.failure_code == "note-unwritten"
            assert launched == [predecessor.session_id], "no second pane"
            assert states[predecessor.session_id] is SessionState.RUNNING
        else:
            assert len(launched) == 2, "exactly one successor"
            successor = row.successor_session_id
            assert successor is not None and successor != predecessor.session_id
            successor_screen = await terminal.capture(successor)
            assert "GOT: Read `" in successor_screen, "the first message was typed into it"
            assert f"NOTE SAYS: {FACT}" in successor_screen
            assert successor in managed
            if mode == "write":
                assert predecessor.session_id not in managed, "the predecessor pane is gone"
                assert states[predecessor.session_id] is SessionState.ENDED
                assert await rollovers.continued_as(predecessor.session_id) == successor
            else:
                assert row.failure_code == "adoption-timeout"
                assert predecessor.session_id in managed, "both panes are alive"
                assert states[predecessor.session_id] is SessionState.RUNNING
        assert not note.exists(), "the note file is removed when the rollover ends"
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
