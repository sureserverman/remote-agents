"""Opt-in native proof: Codex sessions stay on the shared daemon and run with their preset.

Task 4.1 of the codex-permissions-on-the-daemon plan (DEC-139). Codex launches carry no
permission argv, so each pane joins the daemon of its `CODEX_HOME`. The service's own
permission pass (`build_permission_apply_pass`) sets the preset with `thread/settings/update`.
Each turn's `turn_context` is read back from the rollout. That is what the turn ran with,
not what a status line says.

Cases. Each one has its own short `CODEX_HOME`, tmux server, database and spool.

- For each explicit preset: one fresh session, then a resume of its thread with a different
  preset (the next one in `_ROTATION`). The daemon restores a resumed thread's saved
  settings, so resuming with the same preset would pass even with no apply at all. Every
  preset is checked fresh once and resumed once.
- One session through a live change (`change_session_permissions`), then a
  `codex app-server daemon restart` in the drill's own home, after which the pass applies
  again. Then `/new` typed into the pane, to show what a thread the service never bound runs
  with.

Each turn is short and text-only, and asks for no tool: nine turns in all (OD-4).

**What is real.** The `codex` binary, the owner's login (mounted read-only over an empty
placeholder), tmux, and the service's own composition: `_local_runtime`, `compose_backend`,
`build_thread_binder` and `build_permission_apply_pass`, run once a second as `serve` runs them.

**What is stood in.** The client finds its daemon through `codex app-server daemon version`,
which this test runs through the drill's confined wrapper. So it talks only to the drill's
daemon, never to the owner's. There is no Telegram and no `serve` loop: the two passes run in
a background task.

bwrap confines every Codex process (`tests/support/native_isolation.py`), without a PID
namespace, so that panes share one daemon the way the owner's do. **Opt-in**:
`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` or `-m live_acceptance`. Once that is set, a missing
prerequisite FAILS by name. It never skips.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from _pytest.mark.expression import Expression
from agent_panes import answer_hook_review
from codex_home import managed_daemons, retire_codex_home, short_codex_home
from native_isolation import codex_command, sharing_daemon

from remote_agents.adapters.agents.codex.remote_control import AsyncCommandRunner, CommandResult
from remote_agents.adapters.agents.codex.thread_settings import CodexThreadPermissions
from remote_agents.adapters.agents.registry import install_agent_hooks, provider_descriptors
from remote_agents.adapters.agents.thread_binding import thread_of
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.commands import (
    AnswerTrustCommand,
    GracefulStopCommand,
    LaunchCommand,
    ResumeCommand,
)
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.session_folders import SessionFolders
from remote_agents.composition.backend import ProjectCatalogueProvider, compose_backend
from remote_agents.composition.service import build_permission_apply_pass, build_thread_binder
from remote_agents.composition.tui import _local_runtime
from remote_agents.config import load_config, read_claude_limits_source, read_cursor_limits_source
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.trust import TrustState
from remote_agents.ports.terminal import PromptOutcome, PromptReason
from remote_agents.production import ProductionPaths

CODEX = ProfileId("codex")
#: (approval_policy, sandbox type, approvals_reviewer) as `turn_context` records them.
EXPECTED = {
    PermissionPreset.APPROVE_FOR_ME: ("on-request", "workspace-write", "auto_review"),
    PermissionPreset.ASK: ("on-request", "workspace-write", "user"),
    PermissionPreset.FULL_ACCESS: ("never", "danger-full-access", "user"),
}
#: The preset each fresh case's thread is resumed with: never its own, so the apply decides.
_ROTATION = {
    PermissionPreset.APPROVE_FOR_ME: PermissionPreset.ASK,
    PermissionPreset.ASK: PermissionPreset.FULL_ACCESS,
    PermissionPreset.FULL_ACCESS: PermissionPreset.APPROVE_FOR_ME,
}
#: The fixture's own Codex configuration: unlike every preset, so an unapplied turn shows.
_FIXTURE_CONFIG = (
    'approval_policy = "on-request"\nsandbox_mode = "read-only"\napprovals_reviewer = "user"\n'
    'cli_auth_credentials_store = "file"\n[notice]\nhide_rate_limit_model_nudge = true\n'
)
PROMPT = "Reply with exactly PRESET_OBSERVED. Do not use tools or change any file."
_STARTUP_SECONDS = 120.0
_TURN_SECONDS = 120.0
#: How long the pass may take to apply again after the drill's daemon restarts.
_REAPPLY_SECONDS = 120.0
_POLL_SECONDS = 1.0


def _requirements(request: pytest.FixtureRequest) -> tuple[Path, Path]:
    expression = Expression.compile(request.config.option.markexpr or "")
    selected = expression.evaluate(lambda name, **_: name == "live_acceptance") and not (
        expression.evaluate(lambda _name, **_: False)
    )
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1" and not selected:
        pytest.skip("BLOCKED: native acceptance was not requested")
    if hasattr(request.config, "workerinput") or request.config.getoption("numprocesses", 0):
        pytest.fail("BLOCKED: Codex permission qualification must run serially", pytrace=False)
    tools = {name: shutil.which(name) for name in ("codex", "tmux", "bwrap", "git")}
    if not all(tools.values()):
        missing = ", ".join(name for name, path in tools.items() if path is None)
        pytest.fail(f"BLOCKED: {missing} required", pytrace=False)
    source = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        details = source.resolve(strict=True).stat()
    except OSError:
        pytest.fail("BLOCKED: existing Codex file authentication is unavailable", pytrace=False)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        pytest.fail("BLOCKED: Codex authentication is not an owned regular file", pytrace=False)
    return Path(tools["codex"]), Path(tools["bwrap"])


def _key(prefix: str) -> str:
    return f"{prefix}-{date.today()}-{uuid4()}"


class _ThroughWrapper:
    """`CommandRunner` that runs `codex …` as the drill's confined wrapper, in its own home.

    **The socket path crosses a mount boundary.** `<CODEX_HOME>/app-server-control/
    app-server-control.sock` is a symlink into Codex's fixed `/tmp/codex-daemon-<uid>/` (0.169,
    `codex-rs/uds/src/daemon_directory.rs`), which the sandbox shadows with this home's private
    `.native-daemon-locks` (`daemon_lock_mount`) so a drill never writes into the owner's. The
    service's pass runs on the host, where that symlink resolves into the owner's directory and
    finds nothing: every apply read "no Codex daemon" (owner run 2026-11-13T21:17). So the probe's
    `socketPath` is handed on as the file the sandbox mounted there. Everything after -- the
    stat, the owner check, the connect, the handshake, the calls -- is the adapter's own.
    """

    def __init__(self, wrapper: Path, tries: list[str], codex_home: Path) -> None:
        self._wrapper = str(wrapper)
        self._runner = AsyncCommandRunner()
        self._tries = tries
        self._codex_home = codex_home

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        assert argv[0] == "codex", argv
        started = time.time()
        try:
            result = await self._runner.run((self._wrapper, *argv[1:]), timeout=timeout)
        except Exception as error:
            self._tries.append(f"{started:.2f} run {argv[1:]} -> {error!r}")
            raise
        self._tries.append(
            f"{started:.2f} run {argv[1:]} -> exit {result.returncode} "
            f"out {result.stdout.strip()[:400]!r} err {result.stderr.strip()[-400:]!r}"
        )
        if argv[1:] != ("app-server", "daemon", "version") or result.returncode != 0:
            return result
        stdout = _host_socket_path(result.stdout, self._codex_home)
        if stdout != result.stdout:
            self._tries.append(
                f"{started:.2f} socketPath on the host: {json.loads(stdout)['socketPath']}"
            )
        return CommandResult(returncode=result.returncode, stdout=stdout, stderr=result.stderr)


def _host_socket_path(stdout: str, codex_home: Path) -> str:
    """`daemon version`'s JSON with a shadowed `socketPath` as the host sees it, else as is."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout
    if not isinstance(payload, dict) or not isinstance(payload.get("socketPath"), str):
        return stdout
    try:
        target = Path(os.readlink(payload["socketPath"]))
    except OSError:
        return stdout
    if target.parent != Path("/tmp") / f"codex-daemon-{os.getuid()}":
        return stdout
    payload["socketPath"] = str(codex_home / ".native-daemon-locks" / target.name)
    return json.dumps(payload)


class _Drill:
    """One isolated operator home and the service pieces `serve` composes for permissions."""

    def __init__(self, root: Path, codex: Path, codex_home: Path) -> None:
        self.root = root
        self.codex_home = codex_home
        self.repo = Path(tempfile.mkdtemp(prefix="ra-perm-", dir=root)).resolve()
        subprocess.run(
            ["git", "init", "-q", "--template=", str(self.repo)],
            check=True,
            timeout=30,
            capture_output=True,
        )
        home = root / "home"
        self.paths = paths = ProductionPaths.for_home(home)
        paths.ensure_directories(include_unit_directory=False)
        (home / "dev").mkdir()
        registry = home / "registry.yaml"
        registry.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "projects": [
                        {
                            "path": str(self.repo),
                            "name": "permission-fixture",
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
                    f"dev_root = {json.dumps(str(home / 'dev'))}",
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
        # The placeholder stays empty on the host; only confined children see the login.
        argv = sharing_daemon(codex_command(codex, home, codex_home, self.repo, operator_home=home))
        (codex_home / "config.toml").write_text(_FIXTURE_CONFIG, encoding="utf-8")
        install_agent_hooks(
            codex_home / "hooks.json",
            executable=Path(sys.executable),
            activity_directory=paths.activity_directory,
            provider="codex",
        )
        bin_directory = home / ".local" / "bin"
        bin_directory.mkdir(parents=True)
        self.wrapper = bin_directory / "codex"
        self.wrapper.write_text(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n', encoding="utf-8")
        self.wrapper.chmod(0o755)

        config = load_config(paths.config_path)
        self.connection = open_database(paths.database_path, migrations=MIGRATIONS)
        self.projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
        self.projects.refresh()
        #: Each permission try's steps, wall-clock stamped, for the diagnosis to print.
        self.tries: list[str] = []
        self.applier = CodexThreadPermissions(
            _ThroughWrapper(self.wrapper, self.tries, codex_home), home=codex_home
        )
        descriptors = tuple(
            descriptor
            if descriptor.thread_permissions is None
            else replace(descriptor, thread_permissions=self.applier)
            for descriptor in provider_descriptors(
                claude_limits_path=paths.claude_limits_path,
                claude_limits_switch=partial(read_claude_limits_source, paths.config_path),
                claude_home=paths.home,
                cursor_limits_switch=partial(read_cursor_limits_source, paths.config_path),
                cursor_home=paths.home,
            )
        )
        runtime = _local_runtime(
            config,
            paths,
            self.projects.paths,
            descriptors,
            folder_for=SessionFolders(self.projects.paths, None).folder_for,
        )
        self.terminal = runtime.terminal
        self.store = SQLiteSessionStore(self.connection)
        self.bindings = SQLitePermissionBindings(self.connection)
        self.backend = compose_backend(
            config,
            self.connection,
            paths,
            projects=self.projects,
            runtime=runtime,
            store=self.store,
            locks=SessionLocks(),
        )
        self.binder = build_thread_binder(
            connection=self.connection,
            store=self.store,
            terminal=self.terminal,
            descriptors=descriptors,
            activity_directory=paths.activity_directory,
        )
        self.notices: list[str] = []

        async def notify(record, reason: str) -> None:
            self.notices.append(f"{record.session_id}: {reason}")

        self.passes = build_permission_apply_pass(
            store=self.store,
            descriptors=descriptors,
            activity_directory=paths.activity_directory,
            connection=self.connection,
            binder=self.binder,
            notify=notify,
        )
        self.errors: list[str] = []
        (self.project,) = (pid for pid, path in self.projects.paths.items() if path == self.repo)

    async def serve(self, stop: asyncio.Event) -> None:
        """The binder and the permission pass, once a second, as `serve` runs them."""
        while not stop.is_set():
            for step in (self.binder.run_once, self.passes.run_once):
                try:
                    await step()
                except Exception as error:  # the diagnosis prints it; the loop goes on
                    self.errors.append(repr(error))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), _POLL_SECONDS)

    async def diagnosis(self, headline: str) -> str:
        lines = [headline, f"notices: {self.notices}", f"pass errors: {self.errors[-5:]}"]
        lines.append(f"now {time.time():.2f}; tries: {self.tries[-16:]}")
        try:
            lines.append(f"server identity: {await self.applier.server_identity()!r}")
        except Exception as error:
            lines.append(f"server identity: {error!r}")
        lines += self.daemon_evidence()
        with contextlib.suppress(Exception):
            for record in await self.backend.sessions.list_sessions():
                state = await self.bindings.state(record.session_id)
                lines.append(f"session {record.session_id}: {record.state} permissions {state}")
                with contextlib.suppress(Exception):
                    # The whole screen: a "without the shared background server" line sits on top.
                    screen = (await self.terminal.capture(record.session_id)).splitlines()
                    lines.extend(f"  | {line}" for line in screen if line.strip())
        return "\n".join(lines)

    def daemon_evidence(self) -> list[str]:
        """What the drill's own daemon looks like: only this disposable home, never the owner's."""
        lines = []
        try:
            probe = subprocess.run(
                [str(self.wrapper), "app-server", "daemon", "version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            lines.append(f"daemon version: exit {probe.returncode} stdout {probe.stdout.strip()!r}")
            lines.append(f"daemon version stderr: {probe.stderr.strip()[-600:]!r}")
        except Exception as error:
            lines.append(f"daemon version: {error!r}")
        control = self.codex_home / "app-server-control"
        with contextlib.suppress(Exception):
            lines.append(f"control dir: {sorted(item.name for item in control.iterdir())}")
        with contextlib.suppress(Exception):
            lines.append(f"processes from the drill home: {len(managed_daemons(self.codex_home))}")
        with contextlib.suppress(Exception):
            lines.append(f"home: {sorted(item.name for item in self.codex_home.iterdir())}")
        daemon_directory = self.codex_home / "app-server-daemon"
        try:
            # `lstat`: a link into the shadowed /tmp/codex-daemon-<uid> dangles on the host.
            listed = []
            for item in sorted(daemon_directory.iterdir()):
                details = item.lstat()
                link = f" -> {os.readlink(item)}" if stat.S_ISLNK(details.st_mode) else ""
                listed.append(
                    f"{item.name} {stat.filemode(details.st_mode)} uid {details.st_uid} "
                    f"{details.st_size}B mtime {details.st_mtime:.2f}{link}"
                )
            lines.append(f"daemon dir: {listed}")
        except Exception as error:
            lines.append(f"daemon dir: {error!r}")
        for name in ("daemon.pid", "app-server.pid"):
            lines.append(f"{name}: {_pid_file_shape(daemon_directory / name)}")
        for name in ("app-server.stderr.log", "daemon.stderr.log"):
            with contextlib.suppress(Exception):
                lines.append(f"{name}: {(daemon_directory / name).read_text()[-1500:]!r}")
        return lines

    async def until_running(self, session_id: SessionId) -> None:
        started = time.monotonic()
        while True:
            records = await self.backend.sessions.refresh_readiness()
            record = next(item for item in records if item.session_id == session_id)
            screen = await self.terminal.capture(session_id)
            if "Hooks need review" in screen:
                await answer_hook_review(self.terminal, session_id, screen)
            elif record.state is SessionState.RUNNING:
                return
            elif record.state is SessionState.UNTRUSTED:
                answer = await self.backend.sessions.answer_trust(
                    AnswerTrustCommand(session_id, _key("perm-trust"))
                )
                assert answer.observed is not TrustState.AWAITING or (
                    time.monotonic() - started < _STARTUP_SECONDS
                ), await self.diagnosis("the trust question was never answered")
            assert time.monotonic() - started < _STARTUP_SECONDS, await self.diagnosis(
                f"{session_id} is {record.state}, not running"
            )
            await asyncio.sleep(_POLL_SECONDS)

    async def ready(self, session_id: SessionId, preset: PermissionPreset) -> str:
        """Bound, applied, and loaded on the drill's daemon. Returns the session's thread."""
        readiness = await self.passes.wait_ready(session_id, CODEX)
        assert readiness.unapplied is None and readiness.bound, await self.diagnosis(
            f"{session_id} not ready: {readiness}"
        )
        state = await self.bindings.state(session_id)
        assert state is not None and state.applied == preset, await self.diagnosis(
            f"{session_id} applied {state}, not {preset.value}"
        )
        record = await self.store.get(session_id)
        thread = record.resume_source_id or thread_of(
            self.paths.activity_directory, str(session_id)
        )
        assert thread, await self.diagnosis(f"{session_id} has no thread")
        loaded = await self.applier.loaded_threads()
        assert thread in loaded, await self.diagnosis(
            f"{thread} is not loaded on the drill's daemon: {sorted(loaded)}"
        )
        return str(thread)

    async def turn(self, session_id: SessionId, *, other_than: str | None = None) -> dict:
        """One short text turn, and the policy its `turn_context` recorded."""
        before = set(_turns(self.codex_home, self.repo))
        delivered = await _prompt_when_idle(self.terminal, session_id, PROMPT)
        assert delivered.outcome is PromptOutcome.SENT, await self.diagnosis(
            f"the prompt into {session_id} was {delivered.outcome}: {delivered.reason}"
        )
        deadline = time.monotonic() + _TURN_SECONDS
        while time.monotonic() < deadline:
            turns = _turns(self.codex_home, self.repo)
            for turn_id in turns.keys() - before:
                observed = turns[turn_id]
                assert not observed["tools"], "the text-only native probe used a tool"
                if observed["complete"] and observed["thread"] != other_than:
                    return observed
            await asyncio.sleep(0.5)
        pytest.fail(
            await self.diagnosis("BLOCKED: no completed turn_context within the bound"),
            pytrace=False,
        )

    def close(self) -> None:
        self.connection.close()


def _turns(home: Path, workspace: Path) -> dict[str, dict]:
    """Each turn's policy metadata in this workspace, deduplicating resumed rollout history."""
    turns: dict[str, dict] = {}
    for path in sorted((home / "sessions").glob("*/*/*/rollout-*.jsonl")):
        thread = current = None
        matched = False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # an in-progress final line is not evidence
            payload = row.get("payload", {})
            if row.get("type") == "session_meta":
                thread = payload.get("id")
                matched = payload.get("cwd") == str(workspace)
            if not matched:
                continue
            if row.get("type") == "turn_context" and payload.get("turn_id"):
                current = payload["turn_id"]
                turns.setdefault(
                    current,
                    {
                        "thread": thread,
                        "policy": (
                            payload.get("approval_policy"),
                            payload.get("sandbox_policy", {}).get("type"),
                            payload.get("approvals_reviewer"),
                        ),
                        "complete": False,
                        "tools": False,
                    },
                )
            elif current is not None and row.get("type") == "response_item":
                if payload.get("type") in {"function_call", "custom_tool_call"}:
                    turns[current]["tools"] = True
                if payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                    turns[current]["complete"] = True
    return turns


def _pid_file_shape(path: Path) -> str:
    """The pid file as `server_identity` meets it: its kind and owner, and its fields' types.

    Types, not values, beyond the pid and start time: the rest names executables by digest.
    """
    try:
        details = path.lstat()
    except OSError as error:
        return repr(error)
    shape = f"{stat.filemode(details.st_mode)} uid {details.st_uid} {details.st_size}B"
    if stat.S_ISLNK(details.st_mode):
        return f"{shape} -> {os.readlink(path)}"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return f"{shape}; unreadable as JSON: {error!r}"
    if not isinstance(record, dict):
        return f"{shape}; JSON {type(record).__name__}"
    identity = record.get("processIdentity")
    nested = (
        {key: type(value).__name__ for key, value in identity.items()}
        if isinstance(identity, dict)
        else type(identity).__name__
    )
    fields = {key: type(value).__name__ for key, value in record.items()}
    return (
        f"{shape}; fields {fields}; pid {record.get('pid')!r}; "
        f"processStartTime {record.get('processStartTime')!r}; processIdentity {nested}"
    )


def _record_connects(monkeypatch: pytest.MonkeyPatch, tries: list[str]) -> None:
    """Log every Unix socket connect the pass makes: what the path was, and how it ended."""
    real = asyncio.open_unix_connection

    async def recorded(path=None, *args, **kwargs):
        started = time.time()
        try:
            details = os.stat(path)
            seen = f"{stat.filemode(details.st_mode)} uid {details.st_uid}"
        except OSError as error:
            seen = repr(error)
        try:
            opened = await real(path, *args, **kwargs)
        except OSError as error:
            tries.append(f"{started:.2f} connect {path} ({seen}) -> {error!r}")
            raise
        tries.append(f"{started:.2f} connect {path} ({seen}) -> ok")
        return opened

    monkeypatch.setattr(asyncio, "open_unix_connection", recorded)


async def _prompt_when_idle(terminal, session, text, *, timeout=30):
    """Retry only a definitely untouched busy refusal, never an uncertain delivery."""
    deadline = time.monotonic() + timeout
    while True:
        delivered = await terminal.send_prompt(session, text)
        if (
            delivered.outcome is not PromptOutcome.REFUSED
            or delivered.reason not in (PromptReason.BUSY, PromptReason.BUSY_ON_SCREEN)
            or time.monotonic() >= deadline
        ):
            return delivered
        await asyncio.sleep(0.5)


def _policy(observed: dict, preset: PermissionPreset, case: str) -> None:
    assert observed["policy"] == EXPECTED[preset], (
        f"{case}: the turn ran with {observed['policy']}, not {preset.value}'s {EXPECTED[preset]}"
    )


def _report(entry: dict) -> None:
    report = os.environ.get("REMOTE_AGENTS_CODEX_PRESET_REPORT")
    if report:
        descriptor = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(json.dumps(entry) + "\n")


@contextlib.asynccontextmanager
async def _drill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex: Path):
    # Short, because a tmux socket path is capped near 104 bytes and `tmp_path` is long.
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-perm-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
    codex_home = short_codex_home()
    drill = None
    stop = asyncio.Event()
    passes = None
    try:
        drill = _Drill(tmp_path.resolve(), codex, codex_home)
        _record_connects(monkeypatch, drill.tries)
        passes = asyncio.create_task(drill.serve(stop))
        yield drill
    finally:
        stop.set()
        if passes is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(passes, 30)
        # Each step on its own: a tmux that hangs must not leave an authenticated daemon (BL-133).
        with contextlib.suppress(Exception):
            subprocess.run(
                ["tmux", "-S", str(socket), "kill-server"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        retire_codex_home(codex_home)
        if drill is not None:
            with contextlib.suppress(Exception):
                drill.close()
        shutil.rmtree(tmux_directory, ignore_errors=True)


@pytest.mark.live_acceptance
@pytest.mark.parametrize("preset", list(EXPECTED), ids=lambda value: value.value)
async def test_codex_permission_presets_fresh_and_resume(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    preset: PermissionPreset,
) -> None:
    codex, _bwrap = _requirements(request)
    resumed_preset = _ROTATION[preset]
    async with _drill(tmp_path, monkeypatch, codex) as drill:
        fresh = await drill.backend.sessions.launch(
            LaunchCommand(
                drill.project, CODEX, _key("perm-fresh"), "native-fresh", permissions=preset
            )
        )
        session = fresh.record.session_id
        await drill.until_running(session)
        thread = await drill.ready(session, preset)
        observed = await drill.turn(session)
        assert observed["thread"] == thread, "the turn ran on a thread the service did not bind"
        _policy(observed, preset, "fresh")
        stopped = await drill.backend.sessions.graceful_stop(GracefulStopCommand(session, CODEX))
        assert stopped.preserved, await drill.diagnosis("codex did not finish its own exit")

        resolved = ResolvedConversation(
            ConversationSummary(
                ConversationReference("c-" + "a" * 20),
                CODEX,
                drill.project,
                ConversationState.RESUMABLE,
                datetime.now(UTC),
            ),
            ProviderConversationId(thread),
        )
        resumed = await drill.backend.sessions.resume(
            ResumeCommand(
                drill.project,
                CODEX,
                resolved,
                _key("perm-resume"),
                permissions=resumed_preset,
            )
        )
        assert resumed.created
        session = resumed.record.session_id
        await drill.until_running(session)
        assert await drill.ready(session, resumed_preset) == thread
        observed = await drill.turn(session)
        assert observed["thread"] == thread, "resume opened a different native thread"
        _policy(observed, resumed_preset, "resumed")
        _report(
            {
                "case": "fresh_and_resume",
                "preset": preset.value,
                "resumed_with": resumed_preset.value,
                "status": "PASSED",
            }
        )


@pytest.mark.live_acceptance
async def test_codex_live_change_survives_a_daemon_restart_and_new_thread(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _bwrap = _requirements(request)
    async with _drill(tmp_path, monkeypatch, codex) as drill:
        launched = await drill.backend.sessions.launch(
            LaunchCommand(
                drill.project,
                CODEX,
                _key("perm-live"),
                "native-live",
                permissions=PermissionPreset.ASK,
            )
        )
        session = launched.record.session_id
        await drill.until_running(session)
        thread = await drill.ready(session, PermissionPreset.ASK)

        # 1. A live change, as both surfaces make it.
        change = await drill.backend.change_session_permissions(
            session, PermissionPreset.FULL_ACCESS
        )
        assert change.changed, await drill.diagnosis(f"the live change failed: {change}")
        observed = await drill.turn(session)
        assert observed["thread"] == thread
        _policy(observed, PermissionPreset.FULL_ACCESS, "after the live change")

        # 2. The drill's own daemon restarts, as a Codex self-update restarts it.
        before = await drill.applier.server_identity()
        subprocess.run(
            [str(drill.wrapper), "app-server", "daemon", "restart"],
            capture_output=True,
            timeout=60,
            check=True,
        )
        deadline = time.monotonic() + _REAPPLY_SECONDS
        while True:
            state = await drill.bindings.state(session)
            identity = await drill.applier.server_identity()
            if (
                identity is not None
                and identity != before
                and state is not None
                and state.daemon == identity
                and state.applied == PermissionPreset.FULL_ACCESS
            ):
                break
            assert time.monotonic() < deadline, await drill.diagnosis(
                f"no re-apply after the daemon restart: {before} -> {identity}, {state}"
            )
            await asyncio.sleep(_POLL_SECONDS)
        assert thread in await drill.applier.loaded_threads(), await drill.diagnosis(
            "the pane's thread is not loaded on the restarted daemon"
        )
        observed = await drill.turn(session)
        assert observed["thread"] == thread
        _policy(observed, PermissionPreset.FULL_ACCESS, "after the daemon restart")

        # 3. `/new` in the pane: a thread the service never bound or applied to. Measured, not
        # required (OD-16): 0.169's `/new` keeps only permissions the pane itself set
        # (`tui/src/app/new_session.rs`, ExplicitOnly), and the service never re-checks a `/new`
        # thread (Stage 2 residual). What it ran with is reported as a known limitation.
        await drill.terminal._gateway.send_keys(session, ("/new",))
        await asyncio.sleep(1.0)
        await drill.terminal._gateway.send_keys(session, ("Enter",))
        try:
            observed = await drill.turn(session, other_than=thread)
        except (AssertionError, pytest.fail.Exception) as error:
            measured = {"status": "NOT_OBSERVED", "reason": str(error)[-2000:]}
        else:
            measured = {
                "status": "OBSERVED",
                "new_thread": observed["thread"] != thread,
                "policy": observed["policy"],
                "keeps_full_access": observed["policy"] == EXPECTED[PermissionPreset.FULL_ACCESS],
            }
        _report({"case": "live_change_restart_new", "status": "PASSED", "new": measured})
