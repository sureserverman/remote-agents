"""Opt-in live proof: "Rollover with a note" moves a plan-less session to a fresh one (BL-119).

    REMOTE_AGENTS_LIVE_ACCEPTANCE=1 \
        uv run --locked pytest tests/live/test_note_rollover_live.py -q -rA

One case per provider, each in a fresh git repository holding no plan (a session in a plan
folder is sent "Now executing plan" by the planning plugin). The session is told one fact in
its first turn. The owner's press goes through `Backend.rollovers.press`, the call both
surfaces make. The pass in `serve` then asks the idle agent for a note, waits for it, starts a
successor where the session works and types it the note's path, and stops the old session once
the successor has finished its first turn. Asserted: the rollover COMPLETED; the predecessor
ENDED by the pass's stop; and the successor's first answer states the fact (OD-4).

**What is real.** `claude` and `codex`, each under the drill homes `test_rollover_live.py`
builds (private HOME, the owner's login bound read-only, DEC-145); tmux; the service's
composition, its rollover pass on `serve`'s loop and its activity pass on `serve`'s loop, so a
finished turn reaches the store the way a hook's record does.

**What the drill adds.** The agents' activity hooks, installed in the drill home and pointing
at the drill's spool (`install_agent_hooks`), because adoption is read from a finished turn.
Claude's drill settings set `permissions.defaultMode = "acceptEdits"`, as permissive as the
owner's own host default (`auto`), so writing the note raises no approval in a home that holds
none of the owner's settings. Codex runs `workspace-write` with approval `never`, as
`test_rollover_live.py` builds it. A Codex hook review is answered only with the owner's
approval (`REMOTE_AGENTS_LIVE_HOOK_TRUST=owner-approved`, this plan's OD-9).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from codex_home import retire_codex_home, short_codex_home
from native_isolation import claude_command, owner_codex_login

from remote_agents.adapters.agents.registry import (
    install_agent_hooks,
    profiles_that_roll_over_with_a_note,
)
from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.session_actions import NOTE_ROLLOVER, rollover_outcome
from remote_agents.composition.service import (
    _ROLLOVER_POLL_SECONDS,
    _THREAD_BINDING_POLL_SECONDS,
    _bind_threads_periodically,
    _roll_over_periodically,
    _watch_activity_periodically,
)
from remote_agents.config import load_config
from remote_agents.domain.models import ProfileId, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.rollover import TERMINAL, RolloverKind, RolloverState
from remote_agents.production import ProductionPaths

from .test_rollover_live import (
    _answer_trust,
    _codex_prerequisites,
    _compose,
    _git,
    _isolated_codex_home,
    _isolated_home,
    _key,
    _kill_server,
    _owner_fingerprint,
    _prerequisites,
    _stat,
    _type,
    _until_running,
)

_DEADLINE = 12 * 60
_POLL_SECONDS = 2.0
_ACTIVITY_SECONDS = 2.0
_FIRST_TURN_SECONDS = 180


def _planless_repository(parent: Path) -> Path:
    """A fresh repository holding one README and no plan."""
    repo = Path(tempfile.mkdtemp(prefix="ra-note-", dir=parent)).resolve()
    _git(repo, "init", "-q", "--template=", "-b", "main")
    for name, value in (
        ("user.name", "remote-agents live test"),
        ("user.email", "live-test@remote-agents.invalid"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", name, value)
    (repo / "README.md").write_text("# A scratch project\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial commit")
    return repo


def _fact() -> str:
    return f"ochre-{uuid4().hex[:6]}"


async def _note_drive(harness, paths: ProductionPaths, connection, repo: Path, profile, periodic):
    """Tell a session one fact, press Rollover with a note, and check the successor knows it."""
    backend, sessions = harness.backend, harness.backend.sessions
    assert profile in profiles_that_roll_over_with_a_note()
    project_id = next(pid for pid, path in harness.projects.paths.items() if path == repo)
    activity = SQLiteActivityStore(connection)
    composition = replace(
        harness.composition,
        activity_directory=paths.activity_directory,
        activity_store=activity,
        hook_roster=sessions.announce_sessions,
    )
    periodic.append(
        asyncio.create_task(_roll_over_periodically(composition, _ROLLOVER_POLL_SECONDS))
    )
    periodic.append(
        asyncio.create_task(_bind_threads_periodically(composition, _THREAD_BINDING_POLL_SECONDS))
    )
    periodic.append(
        asyncio.create_task(_watch_activity_periodically(composition, _ACTIVITY_SECONDS))
    )

    launched = await sessions.launch(
        LaunchCommand(
            project_id,
            profile,
            _key("note-predecessor"),
            "note-live",
            permissions=PermissionPreset.PROVIDER_DEFAULT if profile == "codex" else None,
        )
    )
    predecessor = launched.record.session_id
    if launched.record.state is SessionState.UNTRUSTED:
        await _answer_trust(harness, predecessor)
    record = await _until_running(harness, predecessor)
    assert await harness.binder.wait_bound(predecessor, profile), "the predecessor never bound"

    fact = _fact()
    told_at = time.monotonic()
    await _type(
        harness,
        predecessor,
        f"Remember this for later: the project's code word is {fact}. Reply with only: noted",
    )
    while not await activity.finished_turn_since(str(predecessor), record.created_at):
        assert time.monotonic() - told_at < _FIRST_TURN_SECONDS, (
            "the first turn never finished:\n" + await harness.terminal.capture(predecessor)
        )
        await asyncio.sleep(_POLL_SECONDS)

    said = await backend.rollovers.press(NOTE_ROLLOVER, record, backend.auto_rollover)
    assert said == rollover_outcome(NOTE_ROLLOVER, done=True), said
    opened = await backend.rollovers.open_for(predecessor)
    assert opened is not None and opened.kind is RolloverKind.NOTE, opened

    started, rollover = time.monotonic(), None
    while time.monotonic() - started < _DEADLINE:
        rollover = await backend.rollovers.latest_for(predecessor)
        if rollover is not None and rollover.state in TERMINAL:
            break
        await asyncio.sleep(_POLL_SECONDS)
    events = [] if rollover is None else await backend.rollovers.events(rollover.id)
    diagnosis = (
        f"{rollover}\n{[(e.to_state.value, e.detail) for e in events]}\n"
        f"notices: {harness.notices}\npredecessor:\n" + await _capture_or_gone(harness, predecessor)
    )
    assert rollover is not None and rollover.state is RolloverState.COMPLETED, diagnosis
    successor = rollover.successor_session_id
    assert successor is not None

    records = {item.session_id: item for item in await sessions.list_sessions()}
    assert records[predecessor].state is SessionState.ENDED, diagnosis
    screen = await harness.terminal.capture(successor)
    assert fact in screen, f"the successor's first answer does not state {fact}:\n{screen}"
    assert not (repo / ".claude" / "handoffs" / f"{rollover.id}.note.md").exists()
    return fact


async def _capture_or_gone(harness, session_id) -> str:
    try:
        return await harness.terminal.capture(session_id)
    except Exception as error:  # the pane may be gone already
        return f"(no pane: {type(error).__name__})"


@pytest.mark.live_acceptance
async def test_rollover_with_a_note_on_a_real_claude(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, _planning = _prerequisites(request.config)
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-note-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
    repo = connection = None
    periodic: list[asyncio.Task] = []
    owners_before = _owner_fingerprint()
    try:
        repo = _planless_repository(tmp_path)
        home = tmp_path / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        activity = ProductionPaths.for_home(home).activity_directory
        settings = tmp_path / "claude-settings.json"
        settings.write_text(
            json.dumps({"permissions": {"defaultMode": "acceptEdits"}}, indent=2) + "\n",
            encoding="utf-8",
        )
        native = claude_command(claude, tmp_path / "native-claude-home", home, repo)
        paths = _isolated_home(
            home,
            repo,
            {"claude": f'exec {shlex.join(native)} --settings {shlex.quote(str(settings))} "$@"'},
        )
        install_agent_hooks(
            settings, executable=Path(sys.executable), activity_directory=paths.activity_directory
        )
        assert paths.activity_directory == activity
        config = load_config(paths.config_path)
        connection = open_database(paths.database_path, migrations=MIGRATIONS)
        harness = _compose(
            config, connection, paths, note_rollable=profiles_that_roll_over_with_a_note()
        )
        await _note_drive(harness, paths, connection, repo, ProfileId("claude"), periodic)
        assert _owner_fingerprint() == owners_before
    finally:
        for task in periodic:
            task.cancel()
        await asyncio.gather(*periodic, return_exceptions=True)
        _kill_server(socket)
        if connection is not None:
            connection.close()
        if repo is not None:
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(tmux_directory, ignore_errors=True)


@pytest.mark.live_acceptance
async def test_rollover_with_a_note_on_a_real_codex(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = _codex_prerequisites(request.config)
    login = owner_codex_login()
    login_before = _stat(login)
    tmux_directory = repo_parent = codex_home = connection = None
    periodic: list[asyncio.Task] = []
    owners_before = _owner_fingerprint()
    try:
        tmux_directory = Path(tempfile.mkdtemp(prefix="ra-note-", dir="/tmp"))
        monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
        repo_parent = Path(tempfile.mkdtemp(prefix="ra-ncx-", dir="/tmp"))
        codex_home = short_codex_home()
        repo = _planless_repository(repo_parent)
        paths = _isolated_codex_home(tmp_path / "home", repo, codex, codex_home)
        install_agent_hooks(
            codex_home / "hooks.json",
            executable=Path(sys.executable),
            activity_directory=paths.activity_directory,
            provider="codex",
        )
        config = load_config(paths.config_path)
        connection = open_database(paths.database_path, migrations=MIGRATIONS)
        harness = _compose(
            config, connection, paths, note_rollable=profiles_that_roll_over_with_a_note()
        )
        await _note_drive(harness, paths, connection, repo, ProfileId("codex"), periodic)
        assert _owner_fingerprint() == owners_before
    finally:
        try:
            for task in periodic:
                task.cancel()
            await asyncio.gather(*periodic, return_exceptions=True)
            if tmux_directory is not None:
                _kill_server(tmux_directory / f"tmux-{os.getuid()}" / "remote-agents")
            if connection is not None:
                connection.close()
            if codex_home is not None and (codex_home / "sessions").is_dir():
                with contextlib.suppress(OSError):
                    shutil.copytree(codex_home / "sessions", tmp_path / "codex-sessions")
        finally:
            try:
                placeholder = None if codex_home is None else codex_home / "auth.json"
                if placeholder is not None and (placeholder.exists() or placeholder.is_symlink()):
                    assert not placeholder.is_symlink()
                    assert placeholder.stat().st_size == 0, "credential placeholder was written"
                assert _stat(login) == login_before, "the owner's auth.json changed"
            finally:
                if codex_home is not None:
                    retire_codex_home(codex_home)
                for made in (repo_parent, tmux_directory):
                    if made is not None:
                        shutil.rmtree(made, ignore_errors=True)
