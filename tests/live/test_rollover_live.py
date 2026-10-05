"""Opt-in live proof: "Rollover now" hands a real plan from one real Claude session to another.

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
`_DEADLINE`, four things are asserted:

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
3. the predecessor is ENDED;
4. the predecessor's row says `continued as #N`, read through `session_views.rollover_marks`
   and `rollover_note`, as both surfaces draw it.

**What is real.** The `claude` binary and the owner's own login, model and permission mode.
The planning plugin, loaded from the coder-plugins checkout (0.55.0 or later: `--adopt-handoff`
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
  real binary with `--plugin-dir <checkout>/planning`, and Claude Code lets that copy override
  the installed one for the session. The pane's `HOME` is still the owner's (the curated
  environment is read from this process), so the login is real.
- **The fixture repository is trusted by the product's own answer.** When the launch comes up
  on the folder-trust dialog, the test answers it through `SessionService.answer_trust`, the
  call behind the bot's trust button. Claude records that answer for the folder, so the
  successor comes up trusted. With `REMOTE_AGENTS_LIVE_PROJECT_ROOT` set, the repository is made
  under that already-trusted root instead, and there is nothing to answer.

**Side effects outside the temporary directories.** Claude writes its own transcript under
`~/.claude/projects/`, and (without a trusted root) a trust entry for the throwaway folder in
`~/.claude.json`. The owner's activity hooks may spool turns from these sessions into the
production spool; the production service does not know the session ids. Those three outlive
the run. The plan itself runs with the owner's real HOME, so the planning skills could in
principle reach the owner's registers: the test fingerprints the project registry and the
vault's Portfolio roll-ups before and after, and fails if either changed. Everything this test
starts is torn down in `finally`: the periodic tasks, the private tmux server and every agent in
it, the database connection and the fixture repository.

**Permissions.** The sessions run in the owner's own Claude permission mode. Under a mode that
asks before writing a file, Stage 1 waits on that question and the run fails at its deadline --
honestly, with the panes in the diagnosis, never as a pass.

**Opt-in.** It runs only when asked: `-m` naming `live_acceptance` (as the gate command above
does), or `REMOTE_AGENTS_LIVE_ACCEPTANCE=1` as for every other live acceptance test. A plain run
skips it, because it spends real model usage and runs for up to fifteen minutes. **Once asked
for, a missing prerequisite FAILS, naming it** -- never a skip: the master gate reads this run,
and a skip there would be a green that proved nothing. The gate still wants `1 passed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import date
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from _pytest.mark.expression import Expression

from remote_agents.adapters.agents.registry import (
    profiles_running_handoffs,
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
from remote_agents.application.session_views import rollover_marks, rollover_note
from remote_agents.bootstrap import _RECONCILE_INTERVAL_SECONDS
from remote_agents.composition.backend import ProjectCatalogueProvider, compose_backend
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


#: The first planning release that writes envelopes and adopts a handoff (coder-plugins DEC-029).
_PLANNING_AT_LEAST = (0, 55, 0)


def _blocked(reason: str) -> None:
    """A prerequisite missing from a run that was asked for: a failure, never a skip."""
    pytest.fail(f"BLOCKED: {reason}", pytrace=False)


def _planning_checkout() -> Path:
    """The planning plugin to load, which must write handoff envelopes and adopt a handoff."""
    configured = os.environ.get("REMOTE_AGENTS_PLANNING_PLUGIN_DIR")
    plugin = (
        Path(configured)
        if configured
        else Path.home() / "dev" / "ai-tools" / "coder-plugins" / "planning"
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
    for needed in ("tmux", "git"):
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


def _isolated_home(root: Path, repo: Path, claude: Path, planning: Path) -> ProductionPaths:
    """A temporary operator home: config with the switch on, a registry naming only `repo`,
    and the `claude` wrapper that loads the planning checkout."""
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
    wrapper = root / ".local" / "bin" / "claude"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(str(claude))} --plugin-dir {shlex.quote(str(planning))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
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
        paths = _isolated_home(tmp_path / "home", repo, claude, planning)
        config = load_config(paths.config_path)
        connection = open_database(paths.database_path, migrations=MIGRATIONS)
        harness = _compose(config, connection, paths)
        await _drive(harness, repo, periodic)
        # Nothing the plan ran reached the owner's own registers.
        assert _owner_fingerprint() == owners_before, (
            "the run changed the owner's project registry or Portfolio roll-ups"
        )
    finally:
        for task in periodic:
            task.cancel()
        await asyncio.gather(*periodic, return_exceptions=True)
        # The explicit socket path, never `-L`: this kills this run's server and nothing else,
        # whatever `TMUX_TMPDIR` happens to say by now.
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            capture_output=True,
            timeout=_GIT_SECONDS,
            check=False,
        )
        if connection is not None:
            connection.close()
        if repo is not None:
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(tmux_directory, ignore_errors=True)


def _owner_fingerprint() -> dict[str, tuple[int, int]]:
    """Size and mtime of the owner's registers the planning skills know: the project registry
    and the vault's Portfolio roll-ups. A file absent on this host is simply not listed."""
    watched = [Path.home() / ".claude" / "projects-registry.yaml"]
    portfolio = Path(os.environ.get("REMOTE_AGENTS_LIVE_VAULT", "/mnt/vault")) / "Portfolio"
    if portfolio.is_dir():
        watched.extend(sorted(portfolio.glob("*.md")))
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
    runtime = _local_runtime(config, paths, projects.paths, descriptors)
    store = SQLiteSessionStore(connection)
    locks = SessionLocks()
    backend = compose_backend(
        config, connection, paths, projects=projects, runtime=runtime, store=store, locks=locks
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
        rollable=profiles_running_handoffs(),
        notify=notify,
    )
    composition = ServiceComposition(
        None,  # no bot boundary: see the module docstring
        runtime.terminal,
        ReconciliationService(store, confirm_ready=runtime.terminal.confirm_ready, locks=locks),
        rollover_pass=rollover_pass,
    )
    return _Harness(backend, runtime.terminal, composition, projects, notices)


async def _drive(harness: _Harness, repo: Path, periodic: list[asyncio.Task]) -> None:
    backend = harness.backend
    sessions = backend.sessions
    claude = ProfileId("claude")
    assert claude in profiles_running_handoffs()
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
        LaunchCommand(project_id, claude, _key("rollover-predecessor"), "rollover-live")
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

    await _type(harness, predecessor, f"/planning:executing-plans {repo / _PLAN}")

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

    # 4. Its row says which session it continued as, as both surfaces draw it.
    marks = await rollover_marks(backend.rollovers, records.values(), sessions=sessions)
    note = rollover_note(marks[str(predecessor)]) if str(predecessor) in marks else None
    expected = f"continued as #{records[successor].display.sequence}"
    assert note is not None and expected in note.split(" · "), f"{note!r} does not say {expected!r}"


async def _answer_trust(harness: _Harness, session_id: SessionId) -> None:
    """Trust the folder through the product's own answer, pressed again while it is unanswered.

    The answer confirms only once the cursor is seen on "Yes" (`TmuxTerminal.answer_trust`).
    Claude draws the dialog before it takes keys, so a press made at once can be left
    unconfirmed -- the 2026-10-05 run found a press at that moment answering "No, exit" -- and
    an owner would press again. So does this, a second apart, within the startup bound.
    """
    started = time.monotonic()
    while True:
        answer = await harness.backend.sessions.answer_trust(
            AnswerTrustCommand(session_id, _key("rollover-trust"))
        )
        if answer.pressed or answer.observed is not TrustState.AWAITING:
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
