"""Serial native policy observations; execution belongs to the final combined gate.

Collection never starts Codex. Opted-in cases run one fresh and one resumed short,
text-only turn, then inspect their actual turn_context metadata. No tool command is
requested. A missing prerequisite is BLOCKED, not a passing skip once opted in.

Each case owns a tmux socket, SQLite database, workspace and short CODEX_HOME.
bwrap makes the host filesystem read-only, binds only those owned directories
writable, and mounts the existing auth.json read-only over an empty placeholder.
There is no auth symlink or credential copy. Model/provider flags and hook trust
are never selected. Native configuration, transcripts and daemons stay isolated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression
from agent_panes import CODEX_OPENING, CODEX_READY, open_to_composer
from codex_home import retire_codex_home, short_codex_home
from native_isolation import daemon_lock_mount

from remote_agents.adapters.agents.registry import profile_composers, profile_trust_dialogs
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile, build_resume_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.commands import GracefulStopCommand, LaunchCommand, ResumeCommand
from remote_agents.application.permission_presets import PermissionSettings, SessionPermissions
from remote_agents.application.services import SessionService
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.profiles import PermissionPreset, closed_profiles
from remote_agents.ports.terminal import PromptOutcome, PromptReason

CODEX = ProfileId("codex")
PROJECT = ProjectId("permission-fixture")
EXPECTED = {
    PermissionPreset.APPROVE_FOR_ME: ("on-request", "workspace-write", "auto_review"),
    PermissionPreset.ASK: ("on-request", "workspace-write", "user"),
    PermissionPreset.FULL_ACCESS: ("never", "danger-full-access", "user"),
    # This is the isolated fixture's explicitly configured provider default.
    PermissionPreset.PROVIDER_DEFAULT: ("on-request", "read-only", "user"),
}
PROMPT = "Reply with exactly PRESET_OBSERVED. Do not use tools or change any file."


def _requirements(request: pytest.FixtureRequest) -> tuple[Path, Path, Path]:
    expression = Expression.compile(request.config.option.markexpr or "")
    selected = expression.evaluate(lambda name, **_: name == "live_acceptance") and not (
        expression.evaluate(lambda _name, **_: False)
    )
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1" and not selected:
        pytest.skip("BLOCKED: native acceptance was not requested")
    if hasattr(request.config, "workerinput") or request.config.getoption("numprocesses", 0):
        pytest.fail("BLOCKED: Codex permission qualification must run serially", pytrace=False)
    tools = [shutil.which(name) for name in ("codex", "tmux", "bwrap")]
    if not all(tools):
        pytest.fail("BLOCKED: codex, tmux and Linux bwrap are required", pytrace=False)
    source = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        auth = source.resolve(strict=True)
        details = auth.stat()
    except OSError:
        pytest.fail("BLOCKED: existing Codex file authentication is unavailable", pytrace=False)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        pytest.fail("BLOCKED: Codex authentication is not an owned regular file", pytrace=False)
    return Path(tools[0]), Path(tools[2]), auth


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _prefix(bwrap: Path, root: Path, home: Path, auth: Path) -> tuple[str, ...]:
    scratch, runtime = home / ".native-tmp", home / ".native-runtime"
    for directory in (scratch, runtime):
        directory.mkdir(mode=0o700)
    return (
        str(bwrap),
        "--setenv",
        "TMPDIR",
        str(scratch),
        "--setenv",
        "XDG_RUNTIME_DIR",
        str(runtime),
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        str(root),
        str(root),
        "--bind",
        str(home),
        str(home),
        *daemon_lock_mount(home),
        "--ro-bind",
        str(auth),
        str(home / "auth.json"),
        "--die-with-parent",
        "--",
    )


def _turns(home: Path, workspace: Path) -> dict[str, dict]:
    """Read only the fixture's policy metadata, deduplicating resumed rollout history."""
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


async def _observe(home: Path, workspace: Path, before: set[str]) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        turns = _turns(home, workspace)
        for turn_id in turns.keys() - before:
            observed = turns[turn_id]
            assert not observed["tools"], "the text-only native probe unexpectedly used a tool"
            if observed["complete"]:
                return observed
        await asyncio.sleep(0.5)
    pytest.fail("BLOCKED: native policy metadata/completed text turn unavailable within 120s")


async def _prompt_when_idle(terminal, session, text, *, timeout=30):
    """Retry only a definitely untouched busy refusal, never an uncertain delivery."""
    deadline = time.monotonic() + timeout
    while True:
        delivered = await terminal.send_prompt(session, text)
        if (
            delivered.outcome is not PromptOutcome.REFUSED
            or delivered.reason is not PromptReason.BUSY
            or time.monotonic() >= deadline
        ):
            return delivered
        await asyncio.sleep(0.5)


def _retire(
    codex: Path,
    prefix: tuple[str, ...],
    environment: dict[str, str],
    home: Path,
) -> None:
    """Stop through the same confinement, then use the shared attested-home cleanup."""

    def stop() -> None:
        try:
            subprocess.run(
                [*prefix, str(codex), "app-server", "daemon", "stop"],
                env=environment,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass

    retire_codex_home(home, stop=stop)


@pytest.mark.live_acceptance
@pytest.mark.parametrize("preset", list(PermissionPreset), ids=lambda value: value.value)
async def test_codex_permission_presets_fresh_and_resume(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    preset: PermissionPreset,
) -> None:
    codex, bwrap, auth = _requirements(request)
    root = tmp_path.resolve()
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", "--template=", str(workspace)], check=True, timeout=15)
    native_home = short_codex_home()
    fixture_home = root / "home"
    fixture_home.mkdir(mode=0o700)
    # The placeholder stays empty on the host. Only the confined child sees the read-only mount.
    (native_home / "auth.json").touch(mode=0o600)
    (native_home / "config.toml").write_text(
        'approval_policy = "on-request"\nsandbox_mode = "read-only"\n'
        'approvals_reviewer = "user"\ncli_auth_credentials_store = "file"\n',
        encoding="utf-8",
    )
    environment = {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TERM") if key in os.environ
    } | {"HOME": str(fixture_home), "CODEX_HOME": str(native_home), "TERM": "xterm-256color"}
    prefix = _prefix(bwrap, root, native_home, auth)
    socket = f"remote-agents-test-permissions-{SessionId.new().value.hex}"
    connection = None
    observations = []
    try:
        holder = _tmux(
            socket, "-f", "/dev/null", "new-session", "-d", "-s", "hold", "-x", "160", "-y", "40"
        )
        assert holder.returncode == 0, "BLOCKED: isolated tmux server could not start"
        version = subprocess.run(
            [*prefix, str(codex), "--version"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        definition = next(item for item in closed_profiles() if item.profile_id == CODEX)

        def confined(profile):
            return replace(profile, executable=str(bwrap), argv=(*prefix, *profile.argv))

        gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=root / "intents")
        terminal = TmuxTerminal(
            gateway,
            {PROJECT: workspace},
            {},
            startup_timeout=25,
            profile_factories={
                CODEX: lambda session, _remote, choice: confined(
                    build_launch_profile(
                        definition, codex, session, environment, permissions=choice
                    )
                )
            },
            resume_profile_factories={
                CODEX: lambda session, source, choice: confined(
                    build_resume_profile(
                        definition, codex, session, source, environment, permissions=choice
                    )
                )
            },
            trust_dialogs=profile_trust_dialogs(),
            composers=profile_composers(),
        )
        config = root / "config.toml"
        config.write_text(f'[codex]\ndefault_permissions = "{preset.value}"\n')
        settings = PermissionSettings(
            ConfigPermissionSetting(config), profiles={CODEX: tuple(PermissionPreset)}
        )
        connection = open_database(root / "sessions.sqlite3")
        store = SQLiteSessionStore(connection)
        bindings = SQLitePermissionBindings(connection)
        service = SessionService(
            store, terminal, permissions=SessionPermissions(settings, bindings, store)
        )
        fresh = await service.launch(LaunchCommand(PROJECT, CODEX, "native-fresh"))
        session = fresh.record.session_id
        source = None
        for kind in ("fresh", "resume"):
            if kind == "resume":
                resolved = ResolvedConversation(
                    ConversationSummary(
                        ConversationReference("c-" + "a" * 20),
                        CODEX,
                        PROJECT,
                        ConversationState.RESUMABLE,
                        datetime.now(UTC),
                    ),
                    ProviderConversationId(source),
                )
                # Seed the association from the thread actually observed above. Native hook
                # attribution is a separate final-gate drill; this one checks preset inheritance.
                await bindings.remember(session, source)
                await settings.set(next(value for value in PermissionPreset if value != preset))
                resumed = await service.resume(
                    ResumeCommand(PROJECT, CODEX, resolved, "native-resume")
                )
                assert resumed.created
                session = resumed.record.session_id
            pane = await gateway.pane_for(session)
            assert pane is not None, "the owned managed pane is missing"
            open_to_composer(
                lambda: _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
                lambda key: _tmux(socket, "send-keys", "-t", pane, key),
                ready=CODEX_READY,
                interstitials=tuple(
                    item for item in CODEX_OPENING if item.marker != "Hooks need review"
                ),
                agent="codex",
                timeout=60,
            )
            await service.refresh_readiness()
            assert (await store.get(session)).state is SessionState.RUNNING
            before = set(_turns(native_home, workspace))
            delivered = await _prompt_when_idle(terminal, session, PROMPT)
            assert delivered.outcome is PromptOutcome.SENT, (
                delivered.reason,
                _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
            )
            observed = await _observe(native_home, workspace, before)
            assert observed["policy"] == EXPECTED[preset], (
                "Native permissions differ: unsupported or managed policy is not qualified.",
                preset.value,
                observed["policy"],
            )
            assert await bindings.for_session(session) == preset
            if source is not None:
                assert observed["thread"] == source, "resume opened a different native thread"
            source = observed["thread"]
            observations.append({"kind": kind, "policy": observed["policy"]})
            stopped = await service.graceful_stop(GracefulStopCommand(session, CODEX))
            assert stopped.preserved, "native agent did not finish its own exit sequence"
        assert len(await store.list()) == 2
    finally:
        try:
            if len(observations) != 2:
                evidence = Path(tempfile.mkdtemp(prefix="native-failure-", dir=root))
                with contextlib.suppress(Exception):
                    (evidence / "pane.capture").write_text(
                        _tmux(socket, "capture-pane", "-p", "-t", pane).stdout
                    )
                    (evidence / "pane.capture").chmod(0o600)
                # Only owned fixture transcripts and native stderr, never auth.json.
                selected = list((native_home / "sessions").glob("*/*/*/rollout-*.jsonl"))
                selected += [native_home / "app-server-daemon/daemon.stderr.log"]
                for source in selected:
                    with contextlib.suppress(OSError):
                        if source.is_symlink() or not source.resolve().is_relative_to(native_home):
                            continue
                        target = evidence / source.relative_to(native_home)
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                        target.chmod(0o600)
        finally:
            try:
                _tmux(socket, "kill-server")
            finally:
                try:
                    _retire(codex, prefix, environment, native_home)
                finally:
                    if connection is not None:
                        connection.close()
    report = os.environ.get("REMOTE_AGENTS_CODEX_PRESET_REPORT")
    if report:
        descriptor = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(
                json.dumps(
                    {
                        "preset": preset.value,
                        "codex_version": version,
                        "observations": observations,
                        "status": "PASSED",
                    }
                )
                + "\n"
            )
