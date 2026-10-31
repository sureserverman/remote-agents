"""Opt-in live proof: "Rollover now" hands a real plan to a fresh session, on Claude and Codex.

The master plan's gate runs this on a real host:

    uv run --locked pytest tests/live/test_rollover_live.py -m live_acceptance -q

**What it does.** It builds a throwaway git repository holding the two-stage fixture plan in
`tests/live/fixtures/rollover/` (one empty file per task, a gate per stage) and launches a real
`claude` in it through the service's own `SessionService.launch`. It presses Rollover now
through `Backend.rollovers.press`, the call both surfaces make, waits for the pass to write the
request, and only then types `/planning:executing-plans <plan>` into the pane through the
terminal's guarded send. The Stage 1 gate then reads the request and hands off with
`rule requested`. The pass in `serve` launches the successor and types the adoption template;
the successor verifies and writes `accepted`, and the pass stops the predecessor. After at most
`_DEADLINE`, five things are asserted:

1. the rollover row is COMPLETED;
2. the successor's `accepted` envelope was written. The envelope itself cannot be read
   afterwards, because a completed rollover discards its envelopes (`RolloverPass._finish`).
   The proof is taken from what survives instead: the rollover's own history holds the
   SUCCESSOR_ACCEPTED move, which `_await_adoption` makes only on an `accepted` envelope whose
   `managed_session_id` is the successor this pass launched; and `.<id>.claim` is still in
   `.claude/handoffs/`, the one-shot claim `handoff-envelope.py accept` creates before it
   writes. The envelopes seen by polling the directory every `_POLL_SECONDS` are reported in
   every failure message, but they are not asserted: a pass can take an envelope between two
   polls, so asserting on it would be flaky;
3. the predecessor is ENDED, by the pass's own stop;
4. its tmux session is gone, read from this run's own tmux server, whose listing must succeed;
5. the predecessor's row says `continued as #N`, read through `session_views.rollover_marks`
   and `rollover_note`, as both surfaces draw it.

**What is real.** The `claude` binary and the owner's own login, model and permission mode. The
planning plugin (staged-planning), loaded from its checkout (0.55.0 or later: `--adopt-handoff`
and envelopes). tmux. git. The service's composition: `compose_backend`, the descriptors and
`_local_runtime` built the way `composition/telegram.py: _private_boundary` builds them, the
rollover pass from `composition/service.py: build_rollover_pass`, and the reconciler. The pass
and the reconciler run on `serve`'s own loop functions (`_roll_over_periodically`,
`_reconcile_periodically`) at `serve`'s own intervals.

**What is stood in, and why.**

- **No Telegram.** A full `serve` long-polls the owner's bot, and a second poller on the same
  token takes updates from the production service (Telegram allows one `getUpdates` caller).
  So the `ServiceComposition` is built without a bot boundary, and only the two loops this
  proof needs are started. The rollover notifier, which tells the owner of failures only, is a
  list the test reads for its failure message.
- **An isolated state directory.** `ProductionPaths` is rooted at a temporary home: its config
  (with `rollover.auto_rollover = true`), database, intents, key locks and spool. The
  registry names only the fixture repository. tmux runs under a private `TMUX_TMPDIR`, so the
  composition's `tmux -L remote-agents` names a server of this run's own, never the owner's.
- **`claude` is reached through a wrapper.** The installed planning plugin may be older than
  0.55.0, and the curated argv is a bare `claude`. So the temporary home's `.local/bin/claude`,
  the first place `_resolve_profile_executable` looks, is a two-line `sh` script. It `exec`s the
  real binary with `--plugin-dir <checkout>/plugins/claude/planning`, and Claude Code lets that
  copy override the installed one for the session. The Claude pane's `HOME` is still the owner's
  (the curated environment is read from this process), so the login is real.
- **The fixture repository is trusted by the product's own answer.** When the launch comes up
  on the folder-trust dialog, the test answers it through `SessionService.answer_trust`, the
  call behind the bot's trust button. Claude records that answer for the folder, so the
  successor comes up trusted. With `REMOTE_AGENTS_LIVE_PROJECT_ROOT` set, the repository is made
  under that already-trusted root instead, and there is nothing to answer.

**Owned state.** Both providers run with private HOME and a read-only host
filesystem. Login credentials are mounted read-only; transcripts, folder trust,
activity and planning records stay in owned fixture storage. Missing credentials,
provider support or explicit hook review remain BLOCKED. Fixture policy is
independent of the normal profile, and no model/provider preference is selected.

**The Codex case** runs the qualified local marketplace named by
`RA_STAGED_PLANNING`, installed only in a short disposable CODEX_HOME. The
existing login is a read-only bwrap mount over an empty non-symlink placeholder.
Only owned fixture roots are writable; HOME, transcripts, hooks, plugin cache and
all generated configuration stay private. No normal model/provider preference or
hook trust is copied. Hook review is an owner prerequisite, never auto-answered.
The source filesystem and normal profile remain read-only. Owned daemon cleanup
uses the retained-directory and pidfd helper even if a pane or test fails.

**Opt-in.** It runs only when asked: `-m` naming `live_acceptance` (as the gate command above
does), or `REMOTE_AGENTS_LIVE_ACCEPTANCE=1` as for every other live acceptance test. A plain run
skips it, because it spends real model usage and runs for up to fifteen minutes. **Once asked
for, a missing prerequisite FAILS, naming it** -- never a skip: the master gate reads this run,
and a skip there would be a green that proved nothing. The gate still wants `2 passed`.
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
from _pytest.mark.expression import Expression
from agent_panes import answer_hook_review
from codex_home import retire_codex_home, short_codex_home
from cross_project_writer import WriterJourney
from native_isolation import claude_command, codex_command

from remote_agents.adapters.agents.registry import (
    adopt_templates,
    profiles_that_roll_over,
    provider_descriptors,
)
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.workflow.handoff_envelopes import FileHandoffEnvelopes
from remote_agents.adapters.workflow.roots import handoff_root
from remote_agents.application.commands import AnswerTrustCommand, LaunchCommand
from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.rollover import RolloverReport
from remote_agents.application.session_actions import ROLLOVER, rollover_outcome
from remote_agents.application.session_folders import SessionFolders
from remote_agents.application.session_views import rollover_marks, rollover_note
from remote_agents.bootstrap import _RECONCILE_INTERVAL_SECONDS
from remote_agents.composition.backend import (
    ProjectCatalogueProvider,
    compose_backend,
    worktree_parts,
)
from remote_agents.composition.service import (
    _ROLLOVER_POLL_SECONDS,
    ServiceComposition,
    _reconcile_periodically,
    _roll_over_periodically,
    build_rollover_pass,
)
from remote_agents.composition.tui import _local_runtime, _resolve_profile_executable
from remote_agents.config import load_config, read_claude_limits_source, read_cursor_limits_source
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.domain.rollover import TERMINAL, RolloverState
from remote_agents.domain.trust import TrustState
from remote_agents.production import ProductionPaths

_MARKER = "live_acceptance"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rollover"
_PLAN = Path("docs") / "rollover-fixture-plan.md"
_BRANCH = "rollover-fixture"

#: The whole run, from typing the plan command: Preflight, Stage 1, its gate, the handoff, a
#: successor's launch and adoption, and the predecessor's stop.
_DEADLINE = 15 * 60
#: How often the test looks at the store and the handoff directory. Not the pass's clock, which
#: is `serve`'s own `_ROLLOVER_POLL_SECONDS`.
_POLL_SECONDS = 2.0
#: How long a launch may take to come up, trusted, at an idle composer.
_STARTUP_SECONDS = 120.0
#: How long the request may take to be written: two of the pass's ticks.
_REQUEST_SECONDS = 2 * _ROLLOVER_POLL_SECONDS + 15
_GIT_SECONDS = 30


def _asked_for(markexpr: str | None) -> bool:
    """Whether `-m <markexpr>` selects a test *because* it carries `live_acceptance`.

    `live_network_gate.asked_for`'s rule for this marker: true when the expression holds for a
    test marked only `live_acceptance` and fails for an unmarked one. So `live_acceptance`
    asks; an empty expression and `not slow` do not, though the second would select the test.
    """
    if not markexpr:
        return False
    try:
        expression = Expression.compile(markexpr)
    except SyntaxError:
        return False

    def only(marker: str | None):
        def matcher(name: str, /, **_kwargs: object) -> bool:
            return name == marker

        return matcher

    return expression.evaluate(only(_MARKER)) and not expression.evaluate(only(None))


def _opted_in(config: pytest.Config) -> bool:
    return os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") == "1" or _asked_for(
        config.getoption("markexpr", "")
    )


#: The first planning release that writes envelopes and adopts a handoff: 0.55.0 of the old
#: Claude package (protocol v1), whose numbering staged-planning's plugin continues.
_PLANNING_AT_LEAST = (0, 55, 0)


def _blocked(reason: str) -> None:
    """A prerequisite missing from a run that was asked for: a failure, never a skip."""
    pytest.fail(f"BLOCKED: {reason}", pytrace=False)


def _planning_checkout() -> Path:
    """The planning plugin to load, which must write handoff envelopes and adopt a handoff."""
    configured = os.environ.get("REMOTE_AGENTS_PLANNING_PLUGIN_DIR")
    if not configured and os.environ.get("RA_STAGED_PLANNING"):
        configured = str(Path(os.environ["RA_STAGED_PLANNING"]) / "plugins/claude/planning")
    plugin = (
        Path(configured)
        if configured
        else Path.home()
        / "dev"
        / "ai-tools"
        / "staged-planning"
        / "plugins"
        / "claude"
        / "planning"
    )
    envelope = plugin / "skills" / "executing-plans" / "scripts" / "handoff-envelope.py"
    skill = plugin / "skills" / "executing-plans" / "SKILL.md"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    if not manifest.is_file() or not envelope.is_file():
        _blocked(f"no planning plugin with handoff envelopes at {plugin}")
    if "--adopt-handoff" not in skill.read_text(encoding="utf-8"):
        _blocked(f"the planning plugin at {plugin} cannot adopt a handoff")
    version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    parts = tuple(int(piece) for piece in version.split(".")[:3] if piece.isdigit())
    if parts < _PLANNING_AT_LEAST:
        # An older plugin ignores the request and adopts nothing: fifteen minutes of waiting
        # that would fail with no word of why.
        _blocked(f"the planning plugin at {plugin} is {version}; the rollover needs 0.55.0+")
    return plugin.resolve()


def _prerequisites(config: pytest.Config) -> tuple[Path, Path]:
    """The real `claude` and the planning checkout, or a skip naming what is missing."""
    if not _opted_in(config):
        pytest.skip(
            "BLOCKED: not asked for; run with `-m live_acceptance` or "
            "REMOTE_AGENTS_LIVE_ACCEPTANCE=1 (a real Claude run, up to 15 minutes)"
        )
    for needed in ("tmux", "git", "bwrap"):
        if shutil.which(needed) is None:
            _blocked(f"executable_missing: {needed}")
    claude = _resolve_profile_executable("claude", Path.home())
    if claude is None:
        _blocked("executable_missing: claude")
    if not (Path.home() / ".claude" / ".credentials.json").is_file():
        _blocked("claude is not logged in (no ~/.claude/.credentials.json)")
    return claude, _planning_checkout()


def _key(prefix: str) -> str:
    return f"{prefix}-{date.today()}-{uuid4()}"


def _git(repo: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        timeout=_GIT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, f"git {' '.join(arguments)}: {completed.stderr}"


def _fixture_repository(parent: Path) -> Path:
    """A fresh repository on its own branch, holding the fixture plan and nothing else.

    `--template=` keeps the owner's git template out: its hooks untrack `docs/plans/` and
    `.claude/`, which would make the plan a file git does not know.
    """
    repo = Path(tempfile.mkdtemp(prefix="ra-rollover-", dir=parent)).resolve()
    _git(repo, "init", "-q", "--template=", "-b", _BRANCH)
    for name, value in (
        ("user.name", "remote-agents live test"),
        ("user.email", "live-test@remote-agents.invalid"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", name, value)
    (repo / _PLAN).parent.mkdir(parents=True)
    shutil.copyfile(_FIXTURES / "fixture-plan.md", repo / _PLAN)
    shutil.copyfile(_FIXTURES / "gitignore", repo / ".gitignore")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial commit: the rollover fixture plan")
    return repo


def _isolated_home(root: Path, repo: Path, wrappers: dict[str, str]) -> ProductionPaths:
    """A temporary operator home: config with the switch on, a registry naming only `repo`,
    and in its `.local/bin` one `sh` wrapper per name, each running its line."""
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
                        "name": "rollover-fixture",
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
                "[rollover]",
                "auto_rollover = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    bin_directory = root / ".local" / "bin"
    bin_directory.mkdir(parents=True)
    for name, line in wrappers.items():
        wrapper = bin_directory / name
        wrapper.write_text(f"#!/bin/sh\n{line}\n", encoding="utf-8")
        wrapper.chmod(0o755)
    return paths


def _codex_prerequisites(config: pytest.Config) -> Path:
    """Use the qualified built writer; never copy normal plugin or hook trust state."""
    if not _opted_in(config):
        pytest.skip("BLOCKED: native acceptance was not requested")
    for needed in ("tmux", "git", "bwrap"):
        if shutil.which(needed) is None:
            _blocked(f"executable_missing: {needed}")
    codex = _resolve_profile_executable("codex", Path.home())
    if codex is None:
        _blocked("executable_missing: codex")
    checkout = os.environ.get("RA_STAGED_PLANNING")
    if not checkout or not (Path(checkout) / ".agents/plugins/marketplace.json").is_file():
        _blocked("RA_STAGED_PLANNING must name the qualified built local marketplace")
    return codex


def _install_codex_plugin(command: tuple[str, ...], root: Path, codex_home: Path) -> None:
    """Install only in the fixture, without setting hook trust or model/provider preferences."""
    checkout = Path(os.environ["RA_STAGED_PLANNING"]).resolve()
    for args in (
        ("plugin", "marketplace", "add", str(checkout), "--json"),
        ("plugin", "add", "planning@staged-planning", "--json"),
    ):
        result = subprocess.run(
            [*command, *args], cwd=root, capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            _blocked(f"isolated local plugin setup failed: {args[:2]} exit{result.returncode}")
    expected = checkout / "plugins/codex/planning/skills/executing-plans/scripts/handoff_v2.py"
    installed = list((codex_home / "plugins/cache").rglob("handoff_v2.py"))
    if not installed or any(path.read_bytes() != expected.read_bytes() for path in installed):
        _blocked("isolated plugin cache does not match the qualified built writer")


def _isolated_codex_home(root: Path, repo: Path, codex: Path, codex_home: Path) -> ProductionPaths:
    """`_isolated_home` for Codex: the same config and registry, a `CODEX_HOME` holding the
    read-only login mount and local plugin, plus isolated entry-point wrappers."""
    # Beside the repository, so it goes with it; the model's only writable folder outside it.
    scratch = repo.parent / "tmp"
    scratch.mkdir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    native_command = codex_command(codex, root, codex_home, repo.parent, operator_home=root)
    paths = _isolated_home(
        root,
        repo,
        {
            # The wrapper's own folder first on `PATH`, here and not in the pane's environment:
            # the composition puts every agent's folder ahead of the inherited `PATH`
            # (`composition/tui.py`, `_local_runtime`), and the owner's `~/.local/bin`, where
            # the installed `remote-agents` lives, sorts before this temporary one.
            "codex": f"HOME={shlex.quote(str(root))} CODEX_HOME={shlex.quote(str(codex_home))} "
            f"TMPDIR={shlex.quote(str(scratch))} "
            f'PATH={shlex.quote(str(root / ".local" / "bin"))}:"$PATH" '
            f'exec {shlex.join(native_command)} "$@"',
            "remote-agents": f'exec {shlex.quote(sys.executable)} -m remote_agents "$@"',
        },
    )
    # The planning skills' portfolio files, under the temporary `HOME` the wrapper sets: an empty
    # vault beside the repository and a registry naming nothing. Without them the decisions scan
    # fails at Preflight (`vault_dir is not configured`) and the executor stops to ask.
    vault = repo.parent / "vault"
    (vault / "Portfolio").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "portfolio-config.yaml").write_text(
        f"version: 1\nvault_dir: {json.dumps(str(vault))}\n", encoding="utf-8"
    )
    (root / ".claude" / "projects-registry.yaml").write_text(
        "version: 1\nprojects: []\n", encoding="utf-8"
    )
    (codex_home / "config.toml").write_text(
        'approval_policy = "never"\nsandbox_mode = "workspace-write"\n'
        'cli_auth_credentials_store = "file"\n'
        "[sandbox_workspace_write]\n"
        f"writable_roots = [{json.dumps(str(repo / '.git'))}, {json.dumps(str(scratch))}]\n"
        "exclude_slash_tmp = true\nexclude_tmpdir_env_var = true\n"
        "\n",
        encoding="utf-8",
    )
    _install_codex_plugin(native_command, root, codex_home)
    return paths


@pytest.mark.live_acceptance
async def test_rollover_now_hands_a_real_plan_to_a_fresh_session(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, planning = _prerequisites(request.config)
    trusted_root = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT_ROOT")
    repo_parent = Path(trusted_root).resolve(strict=True) if trusted_root else tmp_path
    # Short, because a tmux socket path is capped near 104 bytes and `tmp_path` is long.
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-roll-", dir="/tmp"))
    # Set before anything runs tmux: `tmux -L remote-agents` resolves its socket under
    # `$TMUX_TMPDIR/tmux-$UID`, so the composition's own socket name names this run's server.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"

    repo: Path | None = None
    connection = None
    periodic: list[asyncio.Task] = []
    owners_before = _owner_fingerprint()
    try:
        repo = _fixture_repository(repo_parent)
        (tmp_path / "home").mkdir(mode=0o700, exist_ok=True)
        activity = ProductionPaths.for_home(tmp_path / "home").activity_directory
        native_claude = claude_command(
            claude, tmp_path / "native-claude-home", tmp_path / "home", repo
        )
        paths = _isolated_home(
            tmp_path / "home",
            repo,
            {
                # The pane's `HOME` is the owner's, so the verb would record the root the
                # writer reports in the owner's activity directory: point it at this run's.
                "claude": "export PLANNING_SUPERVISOR_ID_CMD="
                f'"$PLANNING_SUPERVISOR_ID_CMD --activity-dir {shlex.quote(str(activity))}"\n'
                f"exec {shlex.join(native_claude)} "
                # `--add-dir` takes many values, so it goes first and "$@" cannot join it.
                f"--add-dir {shlex.quote(str(planning))} "
                f'--plugin-dir {shlex.quote(str(planning))} "$@"'
            },
        )
        config = load_config(paths.config_path)
        connection = open_database(paths.database_path, migrations=MIGRATIONS)
        harness = _compose(config, connection, paths)
        await _drive(
            harness, repo, periodic, ProfileId("claude"), "/planning:executing-plans", socket
        )
        # Nothing the plan ran reached the owner's own registers.
        assert _owner_fingerprint() == owners_before, (
            "the run changed the owner's project registry, Portfolio roll-ups or root reports"
        )
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
async def test_rollover_now_hands_a_real_plan_to_a_fresh_codex_session(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-123 for Codex: the same drive, on a real `codex` (module docstring, the Codex case)."""
    codex = _codex_prerequisites(request.config)
    login = Path.home() / ".codex" / "auth.json"
    tmux_directory: Path | None = None
    repo_parent: Path | None = None
    codex_home: Path | None = None
    connection = None
    periodic: list[asyncio.Task] = []
    owners_before = _owner_fingerprint()
    login_before = _stat(login)
    try:
        # Short, because a tmux socket path is capped near 104 bytes and `tmp_path` is long.
        tmux_directory = Path(tempfile.mkdtemp(prefix="ra-roll-", dir="/tmp"))
        monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
        socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
        repo_parent = Path(tempfile.mkdtemp(prefix="ra-rcx-", dir="/tmp"))
        codex_home = short_codex_home()
        repo = _fixture_repository(repo_parent)
        paths = _isolated_codex_home(tmp_path / "home", repo, codex, codex_home)
        config = load_config(paths.config_path)
        connection = open_database(paths.database_path, migrations=MIGRATIONS)
        harness = _compose(config, connection, paths)
        await _drive(
            harness, repo, periodic, ProfileId("codex"), "$planning:executing-plans", socket
        )
        assert _owner_fingerprint() == owners_before, (
            "the run changed the owner's project registry, Portfolio roll-ups or root reports"
        )
        assert not (codex_home / "auth.json").is_symlink()
        assert (codex_home / "auth.json").stat().st_size == 0, "credential placeholder was written"
        assert _stat(login) == login_before, "Codex rewrote the owner's auth.json"
    finally:
        try:
            for task in periodic:
                task.cancel()
            await asyncio.gather(*periodic, return_exceptions=True)
            if tmux_directory is not None:
                _kill_server(tmux_directory / f"tmux-{os.getuid()}" / "remote-agents")
            if connection is not None:
                connection.close()
            # Codex's own transcripts, the one record of what each tool command printed (a
            # gate's `context-usage.py` among them), kept in pytest's kept `tmp_path`.
            if codex_home is not None and (codex_home / "sessions").is_dir():
                with contextlib.suppress(OSError):
                    shutil.copytree(codex_home / "sessions", tmp_path / "codex-sessions")
        finally:
            # After the panes, and whatever failed above: the daemon the TUI started outlives
            # them (BL-133).
            if codex_home is not None:
                retire_codex_home(codex_home)
            for made in (repo_parent, tmux_directory):
                if made is not None:
                    shutil.rmtree(made, ignore_errors=True)


def _kill_server(socket: Path) -> None:
    """Kill this run's tmux server by its explicit socket path, never `-L`: this server and
    nothing else, whatever `TMUX_TMPDIR` says by now. Never raises, so a hung tmux cannot skip
    the teardown after it."""
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            capture_output=True,
            timeout=_GIT_SECONDS,
            check=False,
        )


def _stat(path: Path) -> tuple[int, int, int]:
    """Inode, size and mtime: enough to see a file rewritten in place or replaced."""
    found = path.stat()
    return found.st_ino, found.st_size, found.st_mtime_ns


def _owner_fingerprint() -> dict[str, tuple[int, int]]:
    """Size and mtime of the owner's registers the planning skills know (the project registry
    and the vault's Portfolio roll-ups), and of every root report in the owner's activity
    directory, which the Claude pane's verb would write under the owner's `HOME`. A file absent
    on this host is simply not listed."""
    watched = [Path.home() / ".claude" / "projects-registry.yaml"]
    portfolio = Path(os.environ.get("REMOTE_AGENTS_LIVE_VAULT", "/mnt/vault")) / "Portfolio"
    if portfolio.is_dir():
        watched.extend(sorted(portfolio.glob("*.md")))
    with contextlib.suppress(Exception):
        roots = ProductionPaths.for_home(Path.home()).activity_directory / "roots"
        watched.extend(sorted(roots.iterdir()))
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in watched
        if path.is_file()
    }


class _Harness:
    """The service's own composition minus Telegram, and what the test reads beside it."""

    def __init__(
        self, backend, terminal, composition: ServiceComposition, projects, notices
    ) -> None:
        self.backend = backend
        self.terminal = terminal
        self.composition = composition
        self.projects = projects
        #: The failure notices the pass handed on -- what the bot would have told the owner.
        self.notices: list[RolloverReport] = notices
        self.seen: set[tuple[str, str, str]] = set()


def _compose(config, connection, paths: ProductionPaths) -> _Harness:
    """`_private_boundary`'s wiring of everything the rollover needs, with no bot boundary."""
    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    descriptors = provider_descriptors(
        claude_context_window=(
            config.claude_context_window if config.claude_context_window_stated else None
        ),
        claude_context_window_stated=config.claude_context_window_stated,
        claude_limits_path=paths.claude_limits_path,
        claude_limits_switch=partial(read_claude_limits_source, config.path or paths.config_path),
        claude_home=paths.home,
        cursor_limits_switch=partial(read_cursor_limits_source, config.path or paths.config_path),
        cursor_home=paths.home,
    )
    projects.refresh()
    # As `_private_boundary`: without the folder bindings every destination is refused.
    worktrees = worktree_parts(connection)
    runtime = _local_runtime(
        config,
        paths,
        projects.paths,
        descriptors,
        folder_for=SessionFolders(projects.paths, worktrees.store, worktrees.folders).folder_for,
    )
    store = SQLiteSessionStore(connection)
    locks = SessionLocks()
    backend = compose_backend(
        config,
        connection,
        paths,
        projects=projects,
        runtime=runtime,
        store=store,
        locks=locks,
        worktrees=worktrees,
    )
    notices: list[RolloverReport] = []

    async def notify(report: RolloverReport) -> None:
        notices.append(report)

    rollover_pass = build_rollover_pass(
        connection=connection,
        sessions=backend.sessions,
        terminal=runtime.terminal,
        enabled=backend.auto_rollover.read,
        project_paths=projects.paths,
        adopt_templates=adopt_templates(),
        notify=notify,
        worktrees=worktrees.store,
        folders=worktrees.folders,
    )
    composition = ServiceComposition(
        None,  # no bot boundary: see the module docstring
        runtime.terminal,
        ReconciliationService(store, confirm_ready=runtime.terminal.confirm_ready, locks=locks),
        rollover_pass=rollover_pass,
    )
    return _Harness(backend, runtime.terminal, composition, projects, notices)


async def _drive(
    harness: _Harness,
    repo: Path,
    periodic: list[asyncio.Task],
    profile: ProfileId,
    executor: str,
    socket: Path,
) -> None:
    """Rollover now on a `profile` session executing the fixture plan through `executor`, the
    plugin's own spelling of its executing-plans skill on that agent. `socket` is this run's
    tmux server."""
    backend = harness.backend
    sessions = backend.sessions
    assert profile in profiles_that_roll_over()
    project_id = next((pid for pid, path in harness.projects.paths.items() if path == repo), None)
    assert project_id is not None, f"the fixture repository is not in the catalogue: {repo}"
    root = handoff_root(repo)
    assert root == repo, f"the handoff root is {root}, not the fixture repository"

    # The service's two loops this proof needs, on `serve`'s own functions and clocks.
    periodic.append(
        asyncio.create_task(_roll_over_periodically(harness.composition, _ROLLOVER_POLL_SECONDS))
    )
    periodic.append(
        asyncio.create_task(
            _reconcile_periodically(harness.composition, _RECONCILE_INTERVAL_SECONDS)
        )
    )

    launched = await sessions.launch(
        LaunchCommand(project_id, profile, _key("rollover-predecessor"), "rollover-live")
    )
    predecessor = launched.record.session_id
    if launched.record.state is SessionState.UNTRUSTED:
        await _answer_trust(harness, predecessor)
    record = await _until_running(harness, predecessor)

    # Rollover now, through the call both surfaces make.
    said = await backend.rollovers.press(ROLLOVER, record, backend.auto_rollover)
    assert said == rollover_outcome(ROLLOVER, done=True), said
    opened = await backend.rollovers.open_for(predecessor)
    assert opened is not None and opened.state is RolloverState.REQUESTED, opened

    # The pass writes the request on its own tick; the plan starts only once it is there, so
    # the Stage 1 gate is certain to read it.
    envelopes = FileHandoffEnvelopes()
    started = time.monotonic()
    while envelopes.requested(root) != str(predecessor):
        assert time.monotonic() - started < _REQUEST_SECONDS, await _diagnosis(
            harness, repo, predecessor, "the pass never wrote the owner's request"
        )
        await asyncio.sleep(_POLL_SECONDS)

    await _type(harness, predecessor, f"{executor} {repo / _PLAN}")

    started = time.monotonic()
    rollover = None
    while time.monotonic() - started < _DEADLINE:
        for envelope in envelopes.events(root):
            harness.seen.add(
                (envelope.event.value, envelope.handoff_id, envelope.managed_session_id)
            )
        rollover = await backend.rollovers.latest_for(predecessor)
        if rollover is not None and rollover.state in TERMINAL:
            break
        await asyncio.sleep(_POLL_SECONDS)

    # 1. A COMPLETED rollover row.
    assert rollover is not None and rollover.state is RolloverState.COMPLETED, await _diagnosis(
        harness, repo, predecessor, f"the rollover did not complete within {_DEADLINE} s"
    )
    successor = rollover.successor_session_id
    assert successor is not None and rollover.handoff_id is not None, rollover

    # 2. The successor's `accepted` was written: the pass's own record of reading it from this
    # successor, and the one-shot claim `accept` leaves behind (see the module docstring).
    history = await backend.rollovers.events(rollover.id)
    assert any(
        event.from_state is RolloverState.ADOPTING
        and event.to_state is RolloverState.SUCCESSOR_ACCEPTED
        for event in history
    ), await _diagnosis(harness, repo, predecessor, "no SUCCESSOR_ACCEPTED move in the history")
    claim = root / ".claude" / "handoffs" / f".{rollover.handoff_id}.claim"
    assert claim.is_file(), await _diagnosis(
        harness, repo, predecessor, f"no accept claim at {claim}"
    )

    # 3. The predecessor ENDED -- by the pass's own graceful stop, not on its own.
    records = {item.session_id: item for item in await sessions.list_sessions()}
    assert records[predecessor].state is SessionState.ENDED, await _diagnosis(
        harness, repo, predecessor, f"the predecessor is {records[predecessor].state}"
    )
    stopped_by_the_pass = [
        event
        for event in history
        if event.from_state is RolloverState.PREDECESSOR_STOPPING
        and event.to_state is RolloverState.COMPLETED
        and event.detail != "the predecessor had already stopped"
    ]
    assert stopped_by_the_pass, await _diagnosis(
        harness, repo, predecessor, "the predecessor ended on its own, not by the pass's stop"
    )

    # 4. Its tmux session is gone -- asked of this run's server, which must answer, so a dead
    # server cannot pass for a stopped pane.
    listing = subprocess.run(
        ["tmux", "-S", str(socket), "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        timeout=_GIT_SECONDS,
        check=False,
    )
    assert listing.returncode == 0, f"this run's tmux server did not answer: {listing.stderr}"
    assert f"ra-{predecessor}" not in listing.stdout.split(), listing.stdout

    # 5. Its row says which session it continued as, as both surfaces draw it.
    marks = await rollover_marks(backend.rollovers, records.values(), sessions=sessions)
    note = rollover_note(marks[str(predecessor)]) if str(predecessor) in marks else None
    expected = f"continued as #{records[successor].display.sequence}"
    assert note is not None and expected in note.split(" · "), f"{note!r} does not say {expected!r}"


async def _answer_trust(harness: _Harness, session_id: SessionId) -> None:
    """Trust the folder through the product's own answer, pressed again while it is unanswered.

    The answer confirms only once the cursor is seen on "Yes" (`TmuxTerminal.answer_trust`).
    Claude draws the dialog, then resets it while it starts (probed 2026-10-05): keys sent to
    the first copy are lost, so a first press can come back pressed with the question still
    up. An owner would press again; so does this, a second apart, until the question is gone,
    within the startup bound.
    """
    started = time.monotonic()
    while True:
        answer = await harness.backend.sessions.answer_trust(
            AnswerTrustCommand(session_id, _key("rollover-trust"))
        )
        if answer.observed is not TrustState.AWAITING:
            return
        assert time.monotonic() - started < _STARTUP_SECONDS, await _diagnosis(
            harness, None, session_id, "the trust question was never answered"
        )
        await asyncio.sleep(1.0)


async def _until_running(harness: _Harness, session_id: SessionId):
    """The record once it is RUNNING; a trust answer is learned through `refresh_readiness`,
    the same re-read the surfaces' lists make."""
    started = time.monotonic()
    while True:
        records = await harness.backend.sessions.refresh_readiness()
        record = next(item for item in records if item.session_id == session_id)
        screen = await harness.terminal.capture(session_id)
        if "Hooks need review" in screen:
            await answer_hook_review(harness.terminal, session_id, screen)
            await asyncio.sleep(1.0)
            continue
        if "Choose the text style that looks best with your terminal" in screen:
            pytest.fail("BLOCKED: private Claude onboarding is not complete", pytrace=False)
        if record.state is SessionState.RUNNING:
            return record
        assert time.monotonic() - started < _STARTUP_SECONDS, await _diagnosis(
            harness, None, session_id, f"the predecessor is {record.state}, not running"
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _type(harness: _Harness, session_id: SessionId, text: str) -> None:
    """Type `text` through the guarded send, waiting out a still-booting agent as the pass's
    own template send does."""
    booting = {DeliveryVerdict.WAIT, DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED}
    started = time.monotonic()
    while True:
        verdict = delivery_verdict(await harness.terminal.send_prompt(session_id, text))
        if verdict is DeliveryVerdict.SENT:
            return
        assert verdict in booting and time.monotonic() - started < _STARTUP_SECONDS, (
            await _diagnosis(harness, None, session_id, f"the plan command was {verdict}")
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _diagnosis(
    harness: _Harness, repo: Path | None, predecessor: SessionId, headline: str
) -> str:
    """Everything a failed live run leaves to read: the rollover, its history, the failure
    notices, the envelopes seen, the sessions and the end of each pane."""
    lines = [headline]
    with contextlib.suppress(Exception):
        rollover = await harness.backend.rollovers.latest_for(predecessor)
        lines.append(f"rollover: {rollover}")
        if rollover is not None:
            for event in await harness.backend.rollovers.events(rollover.id):
                lines.append(
                    f"  {event.from_state} -> {event.to_state}"
                    f" code={event.failure_code} detail={event.detail}"
                )
    lines.append(f"notices: {[report.rollover.failure_code for report in harness.notices]}")
    lines.append(f"envelopes seen: {sorted(harness.seen)}")
    if repo is not None:
        handoffs = repo / ".claude" / "handoffs"
        names = sorted(p.name for p in handoffs.iterdir()) if handoffs.is_dir() else []
        lines.append(f"handoff directory: {names}")
    with contextlib.suppress(Exception):
        for record in await harness.backend.sessions.list_sessions():
            lines.append(f"session {record.session_id} #{record.display.sequence}: {record.state}")
            with contextlib.suppress(Exception):
                tail = (await harness.terminal.capture(record.session_id)).splitlines()[-25:]
                lines.extend(f"  | {line}" for line in tail)
    return "\n".join(lines)


@pytest.mark.live_acceptance
@pytest.mark.parametrize("profile", ["claude", "codex"])
async def test_native_destination_adoption_waits_for_trust_and_preserves_saved_preset(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """Real provider adoption at another registered project; source work is synthetic.

    The parent publishes a genuine built-writer handoff for an owned native idle
    predecessor. This measures destination trust/adoption and persisted routing,
    separately from the native tool-shell identity drill and five-project source matrix.
    """
    checkout = os.environ.get("RA_STAGED_PLANNING")
    if not _opted_in(request.config):
        pytest.skip("BLOCKED: native acceptance was not requested")
    if not checkout:
        _blocked("RA_STAGED_PLANNING must name the qualified built writer")
    if profile == "codex":
        executable = _codex_prerequisites(request.config)
        planning = None
    else:
        executable, planning = _prerequisites(request.config)
    journey = WriterJourney(tmp_path, Path(checkout), profile)
    # Give the native successor a complete, executable fixture plan, with external records.
    target_text = (_FIXTURES / "fixture-plan.md").read_text()
    target_text = target_text.replace(
        "Date: 2026-10-05\n",
        (
            "Date: 2026-10-05\nMaster: ./journey-master-plan.md\n"
            f"Execution root: `{journey.destination}`\nExecution branch: `feature`\n"
        ),
    )
    journey.target_plan.write_text(target_text)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    tmux_directory = Path(tempfile.mkdtemp(prefix="ra-dest-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_directory))
    socket = tmux_directory / f"tmux-{os.getuid()}" / "remote-agents"
    codex_home = None
    connection = None
    try:
        if profile == "codex":
            codex_home = short_codex_home()
            paths = _isolated_codex_home(home, journey.source, executable, codex_home)
        else:
            command = claude_command(executable, home, tmp_path)
            paths = _isolated_home(
                home,
                journey.source,
                {
                    "claude": (
                        f"exec {shlex.join(command)} --add-dir {shlex.quote(str(planning))} "
                        f'--plugin-dir {shlex.quote(str(planning))} "$@"'
                    ),
                    "remote-agents": f'exec {shlex.quote(sys.executable)} -m remote_agents "$@"',
                },
            )
        registry = yaml.safe_load((home / "registry.yaml").read_text())
        registry["projects"].append(
            {
                "path": str(journey.destination),
                "name": "destination",
                "area": "live",
                "enabled": True,
                "added": str(date.today()),
            }
        )
        (home / "registry.yaml").write_text(yaml.safe_dump(registry))
        config = load_config(paths.config_path)
        connection = open_database(paths.database_path, migrations=MIGRATIONS)
        harness = _compose(config, connection, paths)
        sessions = harness.backend.sessions
        # The source is a native idle pane; its completed plan work is synthetic fixture metadata.
        project_id = next(
            pid for pid, path in harness.projects.paths.items() if path == journey.source
        )
        launched = await sessions.launch(
            LaunchCommand(project_id, ProfileId(profile), _key("native-source"))
        )
        predecessor = launched.record.session_id
        if launched.record.state is SessionState.UNTRUSTED:
            await _answer_trust(harness, predecessor)
        record = await _until_running(harness, predecessor)
        preset = await sessions.permission_preset(predecessor)
        journey.predecessor = str(predecessor)
        journey.state(journey.destination, journey.target_plan)
        journey.run()
        rollover_pass = harness.composition.rollover_pass
        assert rollover_pass is not None
        await rollover_pass.run_once()
        row = await harness.backend.rollovers.latest_for(predecessor)
        assert row is not None and row.state is RolloverState.SUCCESSOR_STARTING, await _diagnosis(
            harness,
            journey.source,
            predecessor,
            "BLOCKED: native destination did not present a folder trust hold",
        )
        successor = row.successor_session_id
        assert successor is not None
        assert (await SQLiteSessionStore(connection).get(predecessor)) == record
        assert not list((journey.source / ".claude/handoffs").glob("*.accepted.json"))
        # A retry while trust is unanswered must keep the same successor and source alive.
        await rollover_pass.run_once()
        assert (
            await harness.backend.rollovers.latest_for(predecessor)
        ).successor_session_id == successor
        assert len(await sessions.list_sessions()) == 2
        await _answer_trust(harness, successor)
        started = time.monotonic()
        while time.monotonic() - started < _DEADLINE:
            await sessions.refresh_readiness()
            await rollover_pass.run_once()
            row = await harness.backend.rollovers.latest_for(predecessor)
            if row.state in TERMINAL:
                break
            await asyncio.sleep(_POLL_SECONDS)
        assert row.state is RolloverState.COMPLETED, await _diagnosis(
            harness, journey.source, predecessor, "native destination adoption failed"
        )
        destination_id = next(
            pid for pid, path in harness.projects.paths.items() if path == journey.destination
        )
        records = {item.session_id: item for item in await sessions.list_sessions()}
        assert records[successor].project_id == destination_id
        assert records[predecessor].state is SessionState.ENDED
        assert row.target.destination.root == journey.destination
        assert row.target.destination.branch == "feature"
        assert row.target.destination.plan == str(journey.target_plan)
        assert row.target.destination.locator == "Task 1.1"
        assert await sessions.permission_preset(successor) is preset
        assert len(records) == 2
        history = await harness.backend.rollovers.events(row.id)
        accepted = next(
            n
            for n, event in enumerate(history)
            if event.to_state is RolloverState.SUCCESSOR_ACCEPTED
        )
        stopped = next(
            n
            for n, event in enumerate(history)
            if event.to_state is RolloverState.PREDECESSOR_STOPPING
        )
        assert accepted < stopped
        listing = subprocess.run(
            ["tmux", "-S", str(socket), "list-panes", "-a", "-F", "#{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert listing.returncode == 0 and str(journey.destination) in listing.stdout.splitlines()
    finally:
        _kill_server(socket)
        if connection is not None:
            connection.close()
        if codex_home is not None:
            retire_codex_home(codex_home)
