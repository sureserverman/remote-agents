"""Approved fake-Telegram journey over real SQLite and an isolated tmux server."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_terminal_launch import STARTUP_BUDGET

from remote_agents.adapters.projects.registry import RegisteredProject
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.activity import PaneQuietWatcher
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import build_catalogue
from remote_agents.application.services import SessionService
from remote_agents.application.session_actions import available_actions
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind
from remote_agents.ports.terminal import TerminalTargetMissing


async def test_integrated_fake_journeys_use_real_sqlite_and_isolated_tmux(tmp_path: Path) -> None:
    project_path = tmp_path / "dev" / "opaque-editor"
    project_path.mkdir(parents=True)
    catalogue = build_catalogue(
        (RegisteredProject(project_path, "opaque-editor", "writing"),),
        (),
    )
    project = catalogue[0]
    terminal, gateway = _terminal(tmp_path, ProjectId(project.opaque_id))
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")), terminal
    )

    callbacks = CallbackStateStore()

    try:
        record = await service.launch(
            LaunchCommand(ProjectId(project.opaque_id), ProfileId("claude"), "launch-path")
        )
        assert inspect_capture(await _capture(gateway, record.session_id)).text.startswith("READY")

        stop = StopController(callbacks)
        token = stop.offer(
            record.session_id, record.profile_id, record.state, None, "graceful", 7, 11
        )
        assert token is not None
        callbacks.bind_pending(11, 2)
        request = stop.claim(token, 7, 11, 2)
        assert request is not None
        assert (await stop.execute(request, service, record)).dispatched
        stopped = (await service.list_sessions())[0]
        # One button ended it: the graceful stop removed the tmux session it exited, so
        # there is no pane left to capture and no second step for the owner to confirm.
        assert stopped.state is SessionState.ENDED
        assert available_actions(stopped.state, stopped.orphan_provenance) == ()
        with pytest.raises(TerminalTargetMissing):
            await _capture(gateway, record.session_id)

        command = await service.launch(
            LaunchCommand(ProjectId(project.opaque_id), ProfileId("claude"), "force-path")
        )
        force = StopController(callbacks)
        # The confirmation is a second token carrying a second action, not a flag on the
        # first: a token re-offered onto the same message cannot survive that message's
        # next render.
        assert (
            force.offer(command.session_id, command.profile_id, command.state, None, "force", 7, 11)
            is not None
        )
        token = force.offer_confirmed_force(
            command.session_id, command.profile_id, command.state, None, 7, 11
        )
        assert token is not None
        callbacks.bind_pending(11, 4)
        request = force.claim(token, 7, 11, 4)
        assert request is not None
        assert (await force.execute(request, service, command)).dispatched
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.destroy(record.session_id)
            except RuntimeError:
                pass


async def _capture(gateway: TmuxGateway, session_id) -> bytes:
    return (await gateway.capture(session_id)).encode()


def _terminal(tmp_path: Path, project_id: ProjectId) -> tuple[TmuxTerminal, TmuxGateway]:
    agent = tmp_path / "fake_agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    profile = LaunchProfile(
        sys.executable, (sys.executable, str(agent)), {"PATH": os.environ["PATH"]}, "READY"
    )
    return TmuxTerminal(
        gateway,
        {project_id: tmp_path / "dev" / "opaque-editor"},
        {ProfileId("claude"): profile},
        startup_timeout=STARTUP_BUDGET,
    ), gateway


async def test_stop_returns_to_list_over_real_sqlite_and_tmux(tmp_path: Path) -> None:
    """The goal of Stage 1, proved where nothing is faked but the agent itself.

    The contract tests drive the renderer with a listing double; this one launches a real
    process under tmux, stores it in real SQLite, presses the real stop token through the
    real boundary, and reads the screen that comes back. What it pins is the join: the stop
    ended the session, `_records()` omits an ENDED one, and the landing is therefore the list
    without it — three separate behaviours whose agreement no single-layer test observes.
    """
    project_path = tmp_path / "dev" / "opaque-editor"
    project_path.mkdir(parents=True)
    catalogue = build_catalogue((RegisteredProject(project_path, "opaque-editor", "writing"),), ())
    project = catalogue[0]
    terminal, gateway = _terminal(tmp_path, ProjectId(project.opaque_id))
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")), terminal
    )
    boundary = PrivateBotBoundary(7, 11, catalogue=catalogue, launcher=service)

    try:
        record = await service.launch(
            LaunchCommand(ProjectId(project.opaque_id), ProfileId("claude"), "stop-path")
        )
        assert inspect_capture(await _capture(gateway, record.session_id)).text.startswith("READY")
        listed = await boundary._sessions_reply()
        assert "Sessions 1/1" in listed.text, "it is on the list before the stop"

        token = boundary.stops.offer(
            record.session_id, record.profile_id, record.state, None, "graceful", 7, 11
        )
        assert token is not None
        boundary.callbacks.bind_pending(11, 1)

        reply = await boundary._stop_reply("graceful", token, 1)

        assert (await service.list_sessions())[0].state is SessionState.ENDED
        assert reply["text"].startswith("Stopped ")
        assert "opaque-editor" in reply["text"]
        assert "Nothing is running." in reply["text"], "the list it lands on no longer holds it"
        labels = [button.text for row in reply["reply_markup"].inline_keyboard for button in row]
        assert "Back" not in labels
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.destroy(record.session_id)
            except RuntimeError:
                pass


async def test_instant_launch_reaches_ready_over_real_sqlite_and_tmux(tmp_path: Path) -> None:
    """One press starts a session, and it is READY and unnamed on the list that follows.

    The contract test proves exactly one LaunchCommand is issued. This proves the command
    reaches a real process: a fake agent under an isolated tmux server, its readiness marker
    observed, its row in real SQLite. Between them they cover "one press" and "a session",
    which is the whole of the request and neither test covers alone.
    """
    project_path = tmp_path / "dev" / "opaque-editor"
    project_path.mkdir(parents=True)
    catalogue = build_catalogue((RegisteredProject(project_path, "opaque-editor", "writing"),), ())
    project = catalogue[0]
    terminal, gateway = _terminal(tmp_path, ProjectId(project.opaque_id))
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")), terminal
    )
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=catalogue,
        profiles=(ProfileAvailability("claude", True),),
        launcher=service,
    )

    try:
        # The agent button's own token, minted by the screen that draws it and claimed by the
        # press — the mutation that used to sit on the review screen.
        profiles = await boundary._reply_for("launch.project", project.opaque_id)
        token = next(
            button.callback_data
            for row in profiles["reply_markup"].inline_keyboard
            for button in row
            if button.text == "Claude"
        )

        launched = await boundary._reply_for(
            "launch.profile", f"{project.opaque_id}|claude", token=token
        )

        assert "Session created" in str(launched["text"])
        records = await service.list_sessions()
        assert [record.state for record in records] == [SessionState.RUNNING]
        assert records[0].display.custom_label is None, "one press launches, it does not name"
        assert inspect_capture(await _capture(gateway, records[0].session_id)).text.startswith(
            "READY"
        )
        listed = await boundary._sessions_reply()
        assert "Sessions 1/1" in listed.text
        assert "opaque-editor" in listed.keyboard[0][0].text
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.destroy(record.session_id)
            except RuntimeError:
                pass


async def test_a_real_launch_reorders_the_catalogue_it_was_launched_from(tmp_path: Path) -> None:
    """The seam, end to end: a real launch must be visible to the ranking.

    Every other ranking test hand-builds `ProjectUsage` with an `opaque_id` copied from the
    catalogue, which proves the ranking arithmetic and proves nothing about the join. The join
    is `str(ProjectUsage.project_id) == CatalogProject.opaque_id`, and it holds only because
    the launch path wraps the opaque id as a `ProjectId` before storing it. If that ever
    stopped being true the catalogue would quietly fall back to registry order with every
    existing test green — an unranked list that looks exactly like a correctly ranked one.

    So this launches through the real service into real SQLite, and asserts the boundary's own
    refresh picks it up.
    """
    first = tmp_path / "dev" / "alpha"
    second = tmp_path / "dev" / "opaque-editor"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    catalogue = build_catalogue(
        (
            RegisteredProject(first, "alpha", "writing"),
            RegisteredProject(second, "opaque-editor", "writing"),
        ),
        (),
    )
    beta = next(project for project in catalogue if project.name == "opaque-editor")
    # `_terminal` maps exactly one project id to `dev/opaque-editor`, so the project being
    # launched is the one named for it; `alpha` is registered first and is never launched.
    terminal, gateway = _terminal(tmp_path, ProjectId(beta.opaque_id))
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")), terminal
    )
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=catalogue,
        catalogue_source=lambda: catalogue,
        profiles=(ProfileAvailability("claude", True),),
        launcher=service,
    )
    assert [project.name for project in boundary.catalogue] == ["alpha", "opaque-editor"]

    try:
        await service.launch(
            LaunchCommand(ProjectId(beta.opaque_id), ProfileId("claude"), "rank-path")
        )

        await boundary.refresh_catalogue()

        assert [project.name for project in boundary.catalogue] == ["opaque-editor", "alpha"], (
            "the project just launched from must lead the list it was launched from"
        )
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.destroy(record.session_id)
            except RuntimeError:
                pass


def _chatty_terminal(
    tmp_path: Path, project_id: ProjectId, *, script: str
) -> tuple[TmuxTerminal, TmuxGateway]:
    """A terminal whose fake agent's output behaviour the caller writes."""
    agent = tmp_path / "chatty_agent.py"
    agent.write_text(script, encoding="utf-8")
    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    profile = LaunchProfile(
        sys.executable, (sys.executable, str(agent)), {"PATH": os.environ["PATH"]}, "READY"
    )
    return TmuxTerminal(
        gateway,
        {project_id: tmp_path / "dev" / "opaque-editor"},
        # `codex` rather than `claude`: the watcher deliberately skips the profiles whose own
        # hooks report for them, so launching this as claude would prove nothing.
        {ProfileId("codex"): profile},
        startup_timeout=STARTUP_BUDGET,
    ), gateway


async def _launch_and_watch(
    tmp_path: Path, script: str, *, polls: int
) -> tuple[list, TmuxGateway, object]:
    """Launch a real fake agent into a real tmux and run the watcher over its real pane."""
    project_id = ProjectId("opaque-editor")
    (tmp_path / "dev" / "opaque-editor").mkdir(parents=True)
    terminal, gateway = _chatty_terminal(tmp_path, project_id, script=script)
    with open_database(tmp_path / "state.db") as connection:
        store = SQLiteSessionStore(connection)
        service = SessionService(store, terminal)
        launched = await service.launch(LaunchCommand(project_id, ProfileId("codex"), None))
        assert (await store.get(launched.session_id)).state is SessionState.RUNNING

        watcher = PaneQuietWatcher(store, terminal.capture, quiet_polls=2)
        seen = []
        for _ in range(polls):
            seen.extend(await watcher.poll())
            await asyncio.sleep(0.5)
        return seen, gateway, launched.session_id


async def test_a_quiet_fake_agent_produces_exactly_one_quiet_activity(tmp_path: Path) -> None:
    """The whole path, over a real pane: launch, go silent, be noticed once.

    Every other test of this reaches the classifier with a string a test wrote. This one reads
    what tmux actually captured, which is the only way to find out that a real pane carries a
    trailing cursor line, a prompt, or a redraw that never settles -- any of which would make
    the digest change forever and the signal never fire.
    """
    session_id = None
    gateway = None
    try:
        seen, gateway, session_id = await _launch_and_watch(
            tmp_path,
            # It has to still be working when the watching starts. An agent that is already
            # silent by the first poll is deliberately never reported -- the service cannot
            # tell it from one that finished last week -- so a script that prints everything
            # up front tests the suppression rule and not this one. That is what the first
            # version of this test did, and it correctly saw nothing.
            "import time\n"
            "print('READY', flush=True)\n"
            "for n in range(4):\n"
            "    print(f'step {n}', flush=True)\n"
            "    time.sleep(0.2)\n"
            "time.sleep(30)\n",
            polls=12,
        )

        assert len(seen) == 1, f"expected exactly one quiet activity, got {len(seen)}"
        assert seen[0].kind is ActivityKind.QUIET
        assert seen[0].confidence is ActivityConfidence.INFERRED
        assert str(session_id) == seen[0].session_id
    finally:
        if gateway is not None and session_id is not None:
            try:
                await gateway.destroy(session_id)
            except RuntimeError:
                pass


async def test_a_fake_agent_that_keeps_printing_produces_no_quiet_activity(tmp_path: Path) -> None:
    """A working agent must never be reported as having stopped."""
    session_id = None
    gateway = None
    try:
        seen, gateway, session_id = await _launch_and_watch(
            tmp_path,
            "import itertools, time\n"
            "print('READY', flush=True)\n"
            "for n in itertools.count():\n"
            "    print(f'still working {n}', flush=True)\n"
            "    time.sleep(0.1)\n",
            polls=6,
        )

        assert seen == [], f"a working agent was reported as quiet: {seen}"
    finally:
        if gateway is not None and session_id is not None:
            try:
                await gateway.destroy(session_id)
            except RuntimeError:
                pass
