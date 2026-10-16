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

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import AnswerTrustCommand, LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.provider_descriptor import (
    ComposerScreen,
    ProviderDescriptor,
    TrustDialog,
)
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE
from remote_agents.ports.terminal import PromptOutcome

PROJECT = ProjectId("opaque-editor")
PROFILE = ProfileId("fake")
HANDOFF = "h-0123456789abcdef0123"
#: The planning plugin's own spelling, which the fake agent answers to (`_AGENT`), kept apart
#: from the Claude vertical's `adopt_template` on purpose: the pass types the descriptor's value,
#: so a wrong vertical string is caught here by an agent that never adopts it.
TEMPLATE = f"/planning:executing-plans --adopt-handoff {HANDOFF}"

_AGENT = """
import json, os, sys, time
from pathlib import Path

HANDOFF, MODE = sys.argv[1], Path(sys.argv[2])
ASK = len(sys.argv) > 3 and Path(sys.argv[3]).read_text() == "ask"
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


if ASK:
    # A folder-trust question, drawn before anything else, as Claude draws its own; answered,
    # it is cleared, as Claude's redraw clears it.
    print("FAKE-TRUST-7f3", flush=True)
    print("Do you trust this fake folder?", flush=True)
    print("", flush=True)
    print("› Yes, trust it", flush=True)
    print("  No, exit", flush=True)
    if input() != "":
        sys.exit(0)
    print("\033[2J\033[H", end="", flush=True)

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
        composers={"fake": ProviderDescriptor(PROFILE, "F", "Fake", composer=_COMPOSER)},
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
        adopt_templates={PROFILE: adopt_templates()[ProfileId("claude")]},
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


async def test_a_worktree_sessions_successor_starts_in_the_same_worktree(tmp_path: Path) -> None:
    """BL-116: a session working in its own worktree writes its `ready` there, the pass reads
    it there, and the successor is bound to the same worktree -- its pane starts there, never
    in the project's main checkout."""
    from datetime import UTC, datetime

    from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
    from remote_agents.application.session_folders import SessionFolders
    from remote_agents.domain.models import SessionDisplayIdentity, SessionRecord
    from remote_agents.ports.worktree_store import Worktree

    project = tmp_path / "opaque-editor"
    (project / ".git").mkdir(parents=True)
    worktree = tmp_path / "worktrees" / "wt"
    worktree.mkdir(parents=True)
    # A linked worktree's top level holds a `.git` *file*, which is what makes it a root.
    (worktree / ".git").write_text(f"gitdir: {project}/.git/worktrees/wt\n", encoding="utf-8")
    agent, mode_file = tmp_path / "fake_agent.py", tmp_path / "mode"
    agent.write_text(_AGENT, encoding="utf-8")
    mode_file.write_text("accept", encoding="utf-8")
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteSessionStore(connection)
    worktrees = SQLiteWorktreeStore(connection)
    folders = SessionFolders({PROJECT: project}, worktrees)
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
                (sys.executable, str(agent), HANDOFF, str(mode_file)),
                {"PATH": os.environ["PATH"], SESSION_ID_VARIABLE: str(session_id)},
                "READY",
            )
        },
        composers={"fake": ProviderDescriptor(PROFILE, "F", "Fake", composer=_COMPOSER)},
        folder_for=folders.folder_for,
    )
    sessions = SessionService(store, terminal, worktrees=worktrees)

    async def on() -> bool:
        return True

    rollover_pass = build_rollover_pass(
        connection=connection,
        sessions=sessions,
        terminal=terminal,
        enabled=on,
        project_paths={PROJECT: project},
        adopt_templates={PROFILE: adopt_templates()[ProfileId("claude")]},
        worktrees=worktrees,
    )
    rollovers = SQLiteRolloverStore(connection)
    # The session that made the worktree, long ended: the predecessor reuses its folder, as a
    # resume or an earlier successor does.
    maker = SessionRecord(
        SessionId.new(),
        PROJECT,
        PROFILE,
        SessionDisplayIdentity("opaque-editor", "fake", "regular", 1),
        SessionState.ENDED,
        datetime(2026, 10, 10, tzinfo=UTC),
    )
    await store.save(maker)
    await worktrees.record(
        Worktree(worktree, PROJECT, "ra/wt", "main", "a" * 40, {}, maker.created_at, None),
        bound_to=maker.session_id,
    )
    launched: list[SessionId] = []
    try:
        predecessor = (
            await sessions.launch(
                LaunchCommand(PROJECT, PROFILE, "pred", reuse_folder_of=maker.session_id)
            )
        ).record
        launched.append(predecessor.session_id)
        await _type(terminal, predecessor.session_id, "handoff")

        row = None
        for _ in range(60):
            await rollover_pass.run_once()
            launched.extend(
                record.session_id
                for record in await sessions.list_sessions()
                if record.session_id not in launched and record.session_id != maker.session_id
            )
            rows = rollovers._select("1 = 1", ())
            row = rows[0] if rows else None
            if row is not None and row.state is RolloverState.COMPLETED:
                break
            await asyncio.sleep(0.2)

        assert row is not None and row.state is RolloverState.COMPLETED, (
            None if row is None else (row.state, row.failure_code, row.failure_detail)
        )
        successor = row.successor_session_id
        assert successor is not None
        bound = await worktrees.folder_for(successor)
        assert bound is not None and bound.path == worktree
        intent = (tmp_path / "i" / f"{successor}.json").read_text(encoding="utf-8")
        assert f'"cwd": "{worktree.resolve()}"' in intent, intent
        assert not (project / ".claude").exists(), "nothing was written in the main checkout"
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


#: The fake's folder-trust question, declared as a vertical declares its own -- for the test's
#: profile only. Its cursor rests on the affirmative, so the production answer is one `Enter`.
_TRUST = TrustDialog(
    question="Do you trust this fake folder?",
    affirmative="Yes, trust it",
    negative="No, exit",
    cursor="›",
    identifies_by="FAKE-TRUST-7f3",
)


async def test_a_trust_held_successor_is_typed_once_after_the_owner_trusts_it(
    tmp_path: Path,
) -> None:
    """DEC-125: the successor comes up on its folder-trust question; the rollover holds, types
    nothing into it, and carries on to completion once the owner trusts it from the bot.

    The dialog is the fake's own, declared through the production `TrustDialog` for the test's
    profile, so the production classifier, launch settle and answer path all run on it. Claude's
    real dialog text is pinned against its capture in `tests/provider_contract`, not here."""
    project = tmp_path / "opaque-editor"
    (project / ".git").mkdir(parents=True)
    agent, mode_file, ask = tmp_path / "fake_agent.py", tmp_path / "mode", tmp_path / "ask"
    agent.write_text(_AGENT, encoding="utf-8")
    mode_file.write_text("accept", encoding="utf-8")
    ask.write_text("no", encoding="utf-8")
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
                (sys.executable, str(agent), HANDOFF, str(mode_file), str(ask)),
                {"PATH": os.environ["PATH"], SESSION_ID_VARIABLE: str(session_id)},
                "READY",
            )
        },
        composers={"fake": ProviderDescriptor(PROFILE, "F", "Fake", composer=_COMPOSER)},
        trust_dialogs={str(PROFILE): _TRUST},
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
        adopt_templates={PROFILE: adopt_templates()[ProfileId("claude")]},
    )
    rollovers = SQLiteRolloverStore(connection)
    launched: list[SessionId] = []

    async def walk(until: RolloverState, *, passes: int = 60) -> object:
        row = None
        for _ in range(passes):
            await rollover_pass.run_once()
            launched.extend(
                record.session_id
                for record in await sessions.list_sessions()
                if record.session_id not in launched
            )
            rows = rollovers._select("1 = 1", ())
            row = rows[0] if rows else None
            if row is not None and row.state is until and row.successor_session_id is not None:
                return row
            await asyncio.sleep(0.2)
        raise AssertionError(
            None if row is None else (row.state, row.failure_code, row.failure_detail)
        )

    try:
        predecessor = (await sessions.launch(LaunchCommand(PROJECT, PROFILE, "pred"))).record
        launched.append(predecessor.session_id)
        ask.write_text("ask", encoding="utf-8")  # only the successor is asked
        await _type(terminal, predecessor.session_id, "handoff")

        held = await walk(RolloverState.SUCCESSOR_STARTING)
        successor = held.successor_session_id
        for _ in range(5):  # the hold stands, pass after pass
            await rollover_pass.run_once()
            await asyncio.sleep(0.2)
        (row,) = rollovers._select("1 = 1", ())
        assert row.state is RolloverState.SUCCESSOR_STARTING
        states = {record.session_id: record.state for record in await sessions.list_sessions()}
        assert states[successor] is SessionState.UNTRUSTED
        assert states[predecessor.session_id] is SessionState.RUNNING, "untouched while held"
        screen = await terminal.capture(successor)
        assert "GOT:" not in screen and TEMPLATE not in screen, "nothing typed onto the dialog"
        assert len(launched) == 2, "exactly one successor"

        # The owner presses Trust: the call the bot's button makes, then the readiness re-read
        # the service runs, which is how an answered dialog is noticed.
        answer = await sessions.answer_trust(AnswerTrustCommand(successor, "trust-press-1"))
        assert answer.pressed
        for _ in range(50):
            states = {r.session_id: r.state for r in await sessions.refresh_readiness()}
            if states[successor] is SessionState.RUNNING:
                break
            await asyncio.sleep(0.2)
        assert states[successor] is SessionState.RUNNING

        done = await walk(RolloverState.COMPLETED)
        assert done.successor_session_id == successor
        assert (await terminal.capture(successor)).count(f"GOT: {TEMPLATE}") == 1
        states = {record.session_id: record.state for record in await sessions.list_sessions()}
        assert states[predecessor.session_id] is SessionState.ENDED
        assert await rollovers.continued_as(predecessor.session_id) == successor
        assert len(launched) == 2, "still exactly one successor"
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
