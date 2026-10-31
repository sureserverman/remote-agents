"""Opt-in live proof: after a lost tmux server, real Claude and Codex come back in their own turns.

Task 3.4 of the restart-resume plan (DEC-127). One `claude` and one `codex` session are launched
through the service's own `SessionService.launch`, and each is sent one trivial prompt, so its
hook writes a conversation mark. Once both are idle the tmux server is killed and the Codex
daemon stopped, as a reboot would leave them. Then a fresh composition starts `serve`. Asserted:

1. each session is resumed exactly once, with the production resume argv naming the
   conversation its own mark recorded -- `claude --resume <id>`, `codex resume <id>`;
2. nothing is typed into either, because neither was mid-turn;
3. one restart notice says both came back.

**What is real.** The `claude` and `codex` binaries and the owner's own logins. tmux. The
service's composition: `_local_runtime` and `compose_backend`, built the way
`composition/telegram.py: _private_boundary` builds them, the restart pass from
`composition/service.py: build_restart_resume`, and `_serve_with_reconciliation`, the loop
`serve` runs.

**What is stood in, and why.**

- **No Telegram.** `serve` is given a bot boundary that never polls, for the reason
  `test_rollover_live.py` gives: a second poller on the owner's token takes the production
  service's updates. The notice goes to a list the test reads.
- **An isolated state directory.** `ProductionPaths` is rooted at a temporary home: its config,
  database, intents, key locks and spool. tmux runs under a private `TMUX_TMPDIR`, so the
  composition's `tmux -L remote-agents` names a server of this run's own.
- **The agents are reached through wrappers**, in the temporary home's `.local/bin`, the first
  place `_resolve_profile_executable` looks. `claude` gains `--settings` with this run's hooks.
  `codex` runs with a short disposable `CODEX_HOME` (`tests/support/codex_home.py`) holding a
  read-only mount of the existing login over an empty host placeholder, plus this run's
  `hooks.json`. Both hooks
  write to this run's spool, which is where the pass reads the marks.
- **The folder is trusted by the product's own answer**, as in `test_rollover_live.py`.

**Side effects outside the temporary directories.** Claude writes its transcript under
`~/.claude/projects/` and a trust entry for the throwaway folder in `~/.claude.json`. The owner's
global Claude hooks also fire and may spool these sessions into the production spool, which does
not know their ids. Each turn spends one trivial prompt of the owner's quota (the plan's
Preflight records the owner's approval). Everything this test starts is torn down in `finally`.

**Opt-in**, as every live acceptance test: `REMOTE_AGENTS_LIVE_ACCEPTANCE=1`. Once asked for, a
missing prerequisite FAILS, naming it -- never a skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from agent_panes import answer_hook_review
from codex_home import retire_codex_home, short_codex_home, stop_codex_home
from native_isolation import claude_command, codex_command

from remote_agents.adapters.agents.registry import install_agent_hooks, provider_descriptors
from remote_agents.adapters.agents.turn_markers import (
    FileConversationMarks,
    FileStartedMarks,
    FileTurnMarkers,
)
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.interrupted_resumes import SQLiteInterruptedResumes
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.commands import AnswerTrustCommand, LaunchCommand
from remote_agents.application.interrupted_resume import ResumeReport
from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.session_folders import SessionFolders
from remote_agents.composition.backend import ProjectCatalogueProvider, compose_backend
from remote_agents.composition.service import (
    ServiceComposition,
    _serve_with_reconciliation,
    build_restart_resume,
)
from remote_agents.composition.tui import _local_runtime, _resolve_profile_executable
from remote_agents.config import (
    TelegramSecrets,
    load_config,
    read_claude_limits_source,
    read_cursor_limits_source,
    read_resume_interrupted,
)
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.domain.trust import TrustState
from remote_agents.production import ProductionPaths

_TURN = "Reply with exactly the word: ok"
CLAUDE = ProfileId("claude")
CODEX = ProfileId("codex")
#: How long a launch may take to come up, trusted, at an idle composer.
_STARTUP_SECONDS = 120.0
#: How long one trivial turn may take to end, its mark written.
_TURN_SECONDS = 180.0
#: How long the restart pass may take to resume both.
_PASS_SECONDS = 180.0
_POLL_SECONDS = 1.0


def _blocked(reason: str) -> None:
    """A prerequisite missing from a run that was asked for: a failure, never a skip."""
    pytest.fail(f"BLOCKED: {reason}", pytrace=False)


def _prerequisites() -> tuple[Path, Path]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip(
            "BLOCKED: not asked for; run with REMOTE_AGENTS_LIVE_ACCEPTANCE=1 "
            "(a real Claude and Codex turn each)"
        )
    for needed in ("tmux", "git", "bwrap"):
        if shutil.which(needed) is None:
            _blocked(f"executable_missing: {needed}")
    claude = _resolve_profile_executable("claude", Path.home())
    codex = _resolve_profile_executable("codex", Path.home())
    if claude is None or codex is None:
        _blocked(f"executable_missing: {'claude' if claude is None else 'codex'}")
    if not (Path.home() / ".claude" / ".credentials.json").is_file():
        _blocked("claude is not logged in (no ~/.claude/.credentials.json)")
    if not (Path.home() / ".codex" / "auth.json").is_file():
        _blocked("codex is not logged in (no ~/.codex/auth.json)")
    return claude, codex


def _workspace(parent: Path) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="ra-restart-", dir=parent)).resolve()
    subprocess.run(
        ["git", "init", "-q", "--template=", str(repo)], check=True, timeout=30, capture_output=True
    )
    return repo


def _isolated_home(
    root: Path, repo: Path, claude: Path, codex: Path, codex_home: Path
) -> ProductionPaths:
    """A temporary operator home: config with the restart switch at its default, a registry
    naming only `repo`, and the two wrappers that point each agent's hooks at this run's spool."""
    paths = ProductionPaths.for_home(root)
    paths.ensure_directories(include_unit_directory=False)
    dev_root = root / "dev"
    dev_root.mkdir()
    registry = root / "registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "projects": [
                    {
                        "path": str(repo),
                        "name": "restart-fixture",
                        "area": "live",
                        "enabled": True,
                        "added": str(date.today()),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    paths.config_path.write_text(
        "\n".join(
            (
                "[paths]",
                f"dev_root = {json.dumps(str(dev_root))}",
                f"registry_path = {json.dumps(str(registry))}",
                f"database_path = {json.dumps(str(paths.database_path))}",
                "",
                "[limits]",
                "max_label_length = 40",
                "project_page_size = 10",
                "activity_poll_seconds = 30",
                "",
            )
        ),
        encoding="utf-8",
    )
    spool = paths.activity_directory
    settings = root / "claude-settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    install_agent_hooks(settings, executable=Path(sys.executable), activity_directory=spool)
    codex_argv = codex_command(codex, root, codex_home, repo, operator_home=root)
    claude_argv = claude_command(claude, root, repo)
    (codex_home / "config.toml").write_text(
        'approval_policy = "on-request"\nsandbox_mode = "read-only"\n'
        "[notice]\nhide_rate_limit_model_nudge = true\n",
        encoding="utf-8",
    )
    install_agent_hooks(
        codex_home / "hooks.json",
        executable=Path(sys.executable),
        activity_directory=spool,
        provider="codex",
    )
    bin_directory = root / ".local" / "bin"
    bin_directory.mkdir(parents=True)
    wrappers = {
        "claude": f'exec {shlex.join(claude_argv)} --settings {shlex.quote(str(settings))} "$@"',
        "codex": f'exec {shlex.join(codex_argv)} "$@"',
    }
    for name, line in wrappers.items():
        wrapper = bin_directory / name
        wrapper.write_text(f"#!/bin/sh\n{line}\n", encoding="utf-8")
        wrapper.chmod(0o755)
    return paths


class _Process:
    """One composed service process: what `_private_boundary` wires, minus Telegram."""

    def __init__(self, config, connection, paths: ProductionPaths) -> None:
        self.projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
        self.projects.refresh()
        descriptors = provider_descriptors(
            claude_context_window=(
                config.claude_context_window if config.claude_context_window_stated else None
            ),
            claude_context_window_stated=config.claude_context_window_stated,
            claude_limits_path=paths.claude_limits_path,
            claude_limits_switch=partial(
                read_claude_limits_source, config.path or paths.config_path
            ),
            claude_home=paths.home,
            cursor_limits_switch=partial(
                read_cursor_limits_source, config.path or paths.config_path
            ),
            cursor_home=paths.home,
        )
        self.runtime = _local_runtime(
            config,
            paths,
            self.projects.paths,
            descriptors,
            folder_for=SessionFolders(self.projects.paths, None).folder_for,
        )
        self.terminal = self.runtime.terminal
        self.store = SQLiteSessionStore(connection)
        self.locks = SessionLocks()
        self.backend = compose_backend(
            config,
            connection,
            paths,
            projects=self.projects,
            runtime=self.runtime,
            store=self.store,
            locks=self.locks,
        )
        self.connection = connection
        self.paths = paths
        self.config = config
        self.sends: list[tuple[SessionId, str]] = []
        self.answers: list[str] = []
        self.notices: list[tuple[tuple[ResumeReport, ...], int]] = []
        self.passes = 0

    def project(self, repo: Path):
        found = next((pid for pid, path in self.projects.paths.items() if path == repo), None)
        assert found is not None, f"the workspace is not in the catalogue: {repo}"
        return found

    def composition(self) -> ServiceComposition:
        """The restart pass as `_private_boundary` wires it, with its typing counted."""
        send = self.terminal.send_prompt

        async def counted(session_id: SessionId, text: str):
            self.sends.append((session_id, text))
            delivery = await send(session_id, text)
            self.answers.append(f"{delivery.outcome.value}:{delivery.reason}")
            return delivery

        self.terminal.send_prompt = counted  # type: ignore[method-assign]

        async def enabled() -> bool:
            path = self.config.path or self.paths.config_path
            return await asyncio.to_thread(read_resume_interrupted, path, when_unsure=False)

        async def notify(reports: tuple[ResumeReport, ...], unchecked: int) -> None:
            self.notices.append((reports, unchecked))

        restart = build_restart_resume(
            store=self.store,
            terminal=self.terminal,
            turn_markers=FileTurnMarkers(self.paths.activity_directory),
            conversation_marks=FileConversationMarks(self.paths.activity_directory),
            started_marks=FileStartedMarks(self.paths.activity_directory),
            rollovers=SQLiteRolloverStore(self.connection),
            capabilities=self.backend.conversations.capabilities,
            outcomes=SQLiteInterruptedResumes(self.connection),
            resume=self.backend.sessions.resume,
            folder_of=SQLiteWorktreeStore(self.connection).folder_for,
            enabled=enabled,
            notify=notify,
            stopping=lambda: self.locks.draining,
            lock_path=self.config.database_path.with_name("restart-resume.lock"),
        )
        run = restart.resume.run

        async def counted_pass(snapshot):
            try:
                return await run(snapshot)
            finally:
                self.passes += 1

        object.__setattr__(restart.resume, "run", counted_pass)
        return ServiceComposition(
            build_private_bot(7, 11),
            self.terminal,
            ReconciliationService(
                self.store, confirm_ready=self.terminal.confirm_ready, locks=self.locks
            ),
            restart_resume=restart,
            drain=self.locks.drain,
        )


@pytest.mark.live_acceptance
async def test_a_lost_server_brings_real_claude_and_codex_back_in_their_own_conversations(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, codex = _prerequisites()
    trusted_root = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT_ROOT")
    repo_parent = Path(trusted_root).resolve(strict=True) if trusted_root else tmp_path
    # Short, because a tmux socket path is capped near 104 bytes and `tmp_path` is long.
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-rst-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
    codex_home = short_codex_home()
    repo: Path | None = None
    connections = []
    try:
        repo = _workspace(repo_parent)
        paths = _isolated_home(tmp_path / "home", repo, claude, codex, codex_home)
        config = load_config(paths.config_path)
        connections.append(open_database(paths.database_path, migrations=MIGRATIONS))
        first = _Process(config, connections[-1], paths)
        project_id = first.project(repo)

        # 1. One session of each, each given one trivial turn so a conversation exists.
        launched = {}
        for profile in (CLAUDE, CODEX):
            outcome = await first.backend.sessions.launch(
                LaunchCommand(project_id, profile, _key(f"restart-{profile}"), "restart-live")
            )
            session_id = outcome.record.session_id
            if outcome.record.state is SessionState.UNTRUSTED:
                await _answer_trust(first, session_id)
            await _until_running(first, session_id, socket)
            launched[profile] = session_id
        for session_id in launched.values():
            await _type(first, session_id, _TURN)
        marks = FileConversationMarks(paths.activity_directory)
        markers = FileTurnMarkers(paths.activity_directory)
        conversations = {
            profile: await _turn_ended(first, marks, markers, session_id)
            for profile, session_id in launched.items()
        }

        # 2. The reboot: the server and every pane on it go, and so does the Codex daemon.
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=30)
        _stop_codex_daemon(codex, codex_home)
        await _until_gone(first)

        # 3. A new process starts `serve`.
        connections.append(open_database(paths.database_path, migrations=MIGRATIONS))
        second = _Process(config, connections[-1], paths)

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            deadline = time.monotonic() + _PASS_SECONDS
            while not second.notices and time.monotonic() < deadline:
                await asyncio.sleep(_POLL_SECONDS)

        await _serve_with_reconciliation(
            TelegramSecrets("token", 7, 11), second.composition(), poll, 3600
        )

        assert len(second.notices) == 1, await _diagnosis(second, "no single restart notice")
        ((reports, unchecked),) = second.notices
        assert unchecked == 0, await _diagnosis(second, f"{unchecked} sessions not judged")
        outcomes = {report.record.session_id: report.outcome for report in reports}
        assert set(outcomes) == set(launched.values()), outcomes
        assert all(outcome.startswith("resumed:") for outcome in outcomes.values()), outcomes
        assert not any("not nudged" in outcome for outcome in outcomes.values()), outcomes
        assert second.sends == [], f"nothing is typed into an idle session: {second.sends}"

        records = await second.backend.sessions.list_sessions()
        for profile, argument in ((CLAUDE, "--resume"), (CODEX, "resume")):
            resumed = [
                record
                for record in records
                if record.profile_id == profile and record.state is SessionState.RUNNING
            ]
            assert len(resumed) == 1, await _diagnosis(second, f"{profile} resumed {resumed}")
            (record,) = resumed
            assert record.resume_source_id == conversations[profile], (
                profile,
                record.resume_source_id,
                conversations[profile],
            )
            intent = json.loads(
                (paths.intent_directory / f"{record.session_id}.json").read_text(encoding="utf-8")
            )
            argv = json.dumps(intent)
            assert f'"{argument}", "{conversations[profile]}"' in argv, argv
    finally:
        # Each step on its own: a tmux that hangs past its timeout must not leave an
        # authenticated Codex daemon running from the disposable home (BL-133).
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


#: A turn long enough to be cut off by the lost server and still running in the Codex daemon
#: when `serve` starts again.
_LONG_TURN = "Run the shell command `sleep 40`, then reply with exactly the word: ok"
_SLEEP = ("sleep", "40")


# BL-138 is outside this plan; retain its historical skip, exclude it from the live gate.
@pytest.mark.skip(
    reason="BL-137: fails as measured 2026-10-17 -- the nudge is typed once the surviving turn "
    "ends; reading busy as 'survived' also drops a Codex boot nudge (MCP startup reads busy)"
)
async def test_a_codex_turn_that_outlives_the_server_in_its_daemon_is_not_nudged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tmux crash that spares the Codex daemon: the turn its pane started goes on there, so
    it was not cut off, and `serve` starting while it runs must type nothing into it.

    The closing `not nudged:busy` assumes this turn shows its on-screen busy line at the first
    look; a title-only busy is also a Codex boot (BL-137's next step)."""
    claude, codex = _prerequisites()
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-rsd-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
    codex_home = short_codex_home()
    repo: Path | None = None
    connections = []
    seen: dict[str, object] = {}
    try:
        repo = _workspace(tmp_path)
        paths = _isolated_home(tmp_path / "home", repo, claude, codex, codex_home)
        config = load_config(paths.config_path)
        connections.append(open_database(paths.database_path, migrations=MIGRATIONS))
        first = _Process(config, connections[-1], paths)
        outcome = await first.backend.sessions.launch(
            LaunchCommand(first.project(repo), CODEX, _key("restart-daemon"), "restart-live")
        )
        session_id = outcome.record.session_id
        if outcome.record.state is SessionState.UNTRUSTED:
            await _answer_trust(first, session_id)
        await _until_running(first, session_id, socket)
        await _type(first, session_id, _LONG_TURN)
        markers = FileTurnMarkers(paths.activity_directory)
        await _until_sleeping(first, markers, session_id)

        # The tmux crash: the server and its pane go; the daemon, and the turn in it, stay.
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=30)
        await _until_gone(first)
        seen["after_kill"] = _alive(codex_home)

        connections.append(open_database(paths.database_path, migrations=MIGRATIONS))
        second = _Process(config, connections[-1], paths)

        async def poll(secrets: TelegramSecrets, boundary: PrivateBotBoundary) -> None:
            deadline = time.monotonic() + _PASS_SECONDS
            while not second.notices and time.monotonic() < deadline:
                await asyncio.sleep(_POLL_SECONDS)

        await _serve_with_reconciliation(
            TelegramSecrets("token", 7, 11), second.composition(), poll, 3600
        )
        seen["after_pass"] = _alive(codex_home)
        seen["old_marker"] = markers.started_at(str(session_id))
        seen["answers"] = second.answers
        seen["notices"] = [
            [(str(report.record.session_id), report.outcome) for report in reports]
            for reports, _ in second.notices
        ]
        print("MEASURED", json.dumps(seen, default=str))
        typed = [answer for answer in second.answers if not answer.startswith("refused")]
        assert typed == [], await _diagnosis(
            second, f"typed into a turn that was still running: {seen}"
        )
        ((reports, _),) = second.notices
        (report,) = reports
        assert report.outcome.endswith("; not nudged:busy"), report.outcome
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


def _alive(home: Path | None = None) -> dict[str, int]:
    """How many processes run from the drill's `CODEX_HOME` (the daemon), and how many run
    `sleep 40` (the turn's command), read from `/proc`."""
    daemon = sleeping = 0
    sleep = [part.encode() for part in _SLEEP]
    for entry in Path("/proc").glob("[0-9]*"):
        with contextlib.suppress(OSError):
            if home is not None and os.readlink(entry / "exe").startswith(f"{home}/"):
                daemon += 1
        with contextlib.suppress(OSError):
            if (entry / "cmdline").read_bytes().split(b"\0")[:2] == sleep:
                sleeping += 1
    return {"daemon": daemon, "sleep": sleeping}


async def _until_sleeping(process: _Process, markers: FileTurnMarkers, session_id) -> None:
    """The turn is under way: its marker is down and its `sleep` is running."""
    started = time.monotonic()
    while True:
        if markers.started_at(str(session_id)) is not None and _alive()["sleep"]:
            return
        assert time.monotonic() - started < _TURN_SECONDS, await _diagnosis(
            process, f"{session_id}'s turn never reached its sleep"
        )
        await asyncio.sleep(_POLL_SECONDS)


def _key(prefix: str) -> str:
    return f"{prefix}-{date.today()}-{uuid4()}"


def _stop_codex_daemon(codex: Path, home: Path) -> None:
    """Reap by exact private-home ownership and pidfd, retaining conversation evidence."""
    stop_codex_home(home)


async def _until_gone(process: _Process) -> None:
    started = time.monotonic()
    while True:
        with contextlib.suppress(RuntimeError):
            if await process.terminal.server_started_at() is None:
                return
        assert time.monotonic() - started < 30, "the tmux server did not go away"
        await asyncio.sleep(0.2)


async def _answer_trust(process: _Process, session_id: SessionId) -> None:
    """`test_rollover_live._answer_trust`: pressed again while the question stays up."""
    started = time.monotonic()
    while True:
        answer = await process.backend.sessions.answer_trust(
            AnswerTrustCommand(session_id, _key("restart-trust"))
        )
        if answer.observed is not TrustState.AWAITING:
            return
        assert time.monotonic() - started < _STARTUP_SECONDS, await _diagnosis(
            process, "the trust question was never answered"
        )
        await asyncio.sleep(1.0)


async def _until_running(process: _Process, session_id: SessionId, socket: Path) -> None:
    started = time.monotonic()
    while True:
        records = await process.backend.sessions.refresh_readiness()
        record = next(item for item in records if item.session_id == session_id)
        screen = await process.terminal.capture(session_id)
        if "Hooks need review" in screen:
            await answer_hook_review(process.terminal, session_id, screen)
            await asyncio.sleep(1.0)
            continue
        if "Choose the text style that looks best with your terminal" in screen:
            pytest.fail("BLOCKED: private Claude onboarding is not complete", pytrace=False)
        if record.state is SessionState.RUNNING:
            return
        if record.state is SessionState.UNTRUSTED:
            screen = await process.terminal.capture(session_id)
            if "Hooks need review" in screen:
                await answer_hook_review(process.terminal, session_id, screen)
                await asyncio.sleep(1.0)
                continue
            await _answer_trust(process, session_id)
        assert time.monotonic() - started < _STARTUP_SECONDS, await _diagnosis(
            process, f"{session_id} is {record.state}, not running"
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _type(process: _Process, session_id: SessionId, text: str) -> None:
    """Type through the guarded send, waiting out a still-booting agent."""
    booting = {DeliveryVerdict.WAIT, DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED}
    started = time.monotonic()
    while True:
        verdict = delivery_verdict(await process.terminal.send_prompt(session_id, text))
        if verdict is DeliveryVerdict.SENT:
            return
        assert verdict in booting and time.monotonic() - started < _STARTUP_SECONDS, (
            await _diagnosis(process, f"the prompt into {session_id} was {verdict}")
        )
        await asyncio.sleep(_POLL_SECONDS * 2)


async def _turn_ended(
    process: _Process, marks: FileConversationMarks, markers: FileTurnMarkers, session_id
) -> str:
    """The conversation the session's hook marked, once its turn marker has ended."""
    started = time.monotonic()
    while True:
        mark = marks.latest(str(session_id))
        if mark is not None and markers.started_at(str(session_id)) is None:
            return mark
        assert time.monotonic() - started < _TURN_SECONDS, await _diagnosis(
            process, f"{session_id}'s turn did not end with a mark (mark: {mark})"
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _diagnosis(process: _Process, headline: str) -> str:
    """The notices, the sessions and the end of each pane."""
    lines = [headline, f"notices: {process.notices}", f"sends: {process.sends}"]
    with contextlib.suppress(Exception):
        for record in await process.backend.sessions.list_sessions():
            lines.append(f"session {record.session_id} {record.profile_id}: {record.state}")
            with contextlib.suppress(Exception):
                tail = (await process.terminal.capture(record.session_id)).splitlines()[-20:]
                lines.extend(f"  | {line}" for line in tail)
    return "\n".join(lines)
