"""The envelope reader agrees with each planning plugin's own writer, run for real.

The protocol's writer has three implementations: staged-planning's `handoff-envelope.py`, the
planning plugin's own; coder-plugins' copy for Claude Code; and engineering-skills' Codex fork
of that copy. Each writes the envelopes and reads `request.json`; `adapters/workflow` does the
reverse. Fixtures written
by hand here would only prove the reader agrees with this file's idea of the protocol; this
runs the other side's script into a temporary git repository and reads what it actually wrote.
It also writes a tampered copy of every envelope kind beside the real one, so a reader that
accepted anything shaped like JSON would fail here and not just in the security suite.

Each writer is a parameter:
- `staged-planning` runs the built
  `staged-planning/plugins/claude/planning/skills/executing-plans/scripts/handoff-envelope.py`
  from a checkout, never a copy in this repo. It names its session by running
  `$PLANNING_SUPERVISOR_ID_CMD`, here an absolute stub path, with a decoy `remote-agents`
  earlier on `PATH` that must not answer. It reads and writes only `planning-handoff`, and it
  reports the root it writes under to that command as `PLANNING_HANDOFF_ROOT`.
- `claude` runs coder-plugins' script, naming its session by `REMOTE_AGENTS_SESSION_ID`. It
  stays until the cutover (staged-planning master Sub-plan 7) retires it.
- `codex` runs engineering-skills' fork, which names its session by asking
  `remote-agents session-id` (DEC-128): a stub of that command on `PATH` prints the id.
For the two writers that ask a command, the shell carries a different
`REMOTE_AGENTS_SESSION_ID` throughout -- the daemon starter's, as measured -- which they must
never stamp.

A writer is skipped unless its variable names a checkout (`RA_STAGED_PLANNING`,
`RA_CODER_PLUGINS`, `RA_ENGINEERING_SKILLS`), so CI -- which has none -- is unaffected. Set but
pointing at no script is a failure, not a skip: a gate run with the variable set is claiming
this test ran.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from remote_agents.adapters.agents.reported_roots import FileReportedRoots
from remote_agents.adapters.tmux.profiles import supervisor_id_command
from remote_agents.adapters.workflow.handoff_envelopes import (
    LEGACY_PROTOCOL,
    PROTOCOL,
    FileHandoffEnvelopes,
)
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.domain.models import SessionId
from remote_agents.ports.handoff_envelopes import HandoffEvent
from remote_agents.production import ProductionPaths

SESSION = "ra-compat_1"
#: What a Codex tool shell inherits from the pane that started the shared daemon (DEC-128).
DAEMON_STARTER = "ra-starter_9"

#: The identity command a writer runs: the `remote-agents` a Codex writer finds on PATH, or
#: the `id-stub` the staged-planning writer is told of. It prints the session it is told to, or
#: exits as told. Each call appends what it was given to `$RA_STUB_SEEN`: the reported root
#: (`-` when unset) and its argv, one per line. The real verb's rule is tested in this repo;
#: here only the writer's use of it.
_STUB = """#!/bin/sh
if [ -n "$RA_STUB_SEEN" ]; then
  printf 'root %s\n' "${PLANNING_HANDOFF_ROOT--}" >> "$RA_STUB_SEEN"
  printf 'argv %s\n' "$*" >> "$RA_STUB_SEEN"
fi
if [ -n "$RA_STUB_EXIT" ]; then exit "$RA_STUB_EXIT"; fi
if [ "$1" != "session-id" ] || [ "$#" -ne 1 ]; then exit 2; fi
printf '%s\n' "$RA_STUB_SESSION"
"""
#: A `remote-agents` earlier on the staged-planning writer's `PATH`: a writer that looked the
#: verb up there, not through its command variable, would stamp this id and fail the readback.
_DECOY = "#!/bin/sh\nprintf 'ra-decoy_0\\n'\n"
_DECOY_SESSION = "ra-decoy_0"
_ID_COMMAND = "PLANNING_SUPERVISOR_ID_CMD"
_ROOT_VARIABLE = "PLANNING_HANDOFF_ROOT"


@dataclass(frozen=True)
class Writer:
    name: str
    variable: str
    relative: str
    asks_the_verb: bool
    #: Names its session by running `$PLANNING_SUPERVISOR_ID_CMD` (staged-planning's writer).
    runs_the_id_command: bool = False
    #: Reads only `remote-agents-handoff`. The coder-plugins and Codex writers do, until the
    #: staged-planning cutover (master Sub-plan 7) retires them.
    legacy: bool = True


_WRITERS = {
    "staged-planning": Writer(
        "staged-planning",
        "RA_STAGED_PLANNING",
        "plugins/claude/planning/skills/executing-plans/scripts/handoff-envelope.py",
        asks_the_verb=False,
        runs_the_id_command=True,
        legacy=False,
    ),
    "claude": Writer(
        "claude",
        "RA_CODER_PLUGINS",
        "planning/skills/executing-plans/scripts/handoff-envelope.py",
        asks_the_verb=False,
    ),
    "codex": Writer(
        "codex",
        "RA_ENGINEERING_SKILLS",
        "ports/codex/skills/executing-plans/scripts/handoff-envelope.py",
        asks_the_verb=True,
    ),
}


@dataclass(frozen=True)
class Harness:
    writer: Writer
    script: Path
    stub_dir: Path
    decoy_dir: Path

    @property
    def stub(self) -> Path:
        return self.stub_dir / ("id-stub" if self.writer.runs_the_id_command else "remote-agents")

    @property
    def seen(self) -> Path:
        """What the stub was given, one `root` and one `argv` line per call."""
        return self.stub_dir / "seen"

    def run(
        self,
        root: Path,
        *argv: str,
        session: str | None = SESSION,
        stub_exit: int | None = None,
        cwd: Path | None = None,
        overrides: dict[str, str | None] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run the writer with `--root root`, in `cwd` (default `root`); `overrides` sets or,
        with None, removes variables after the harness has set its own."""
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "REMOTE_AGENTS_SESSION_ID",
                "RA_STUB_SESSION",
                "RA_STUB_EXIT",
                "RA_STUB_SEEN",
                _ID_COMMAND,
                _ROOT_VARIABLE,
            }
        }
        if self.writer.asks_the_verb or self.writer.runs_the_id_command:
            if self.writer.asks_the_verb:
                env["PATH"] = f"{self.stub_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
            else:
                env["PATH"] = f"{self.decoy_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
                env[_ID_COMMAND] = shlex.join([str(self.stub), "session-id"])
            env["REMOTE_AGENTS_SESSION_ID"] = DAEMON_STARTER
            env["RA_STUB_SEEN"] = str(self.seen)
            if session is None:
                env["RA_STUB_EXIT"] = "3"
            else:
                env["RA_STUB_SESSION"] = session
            if stub_exit is not None:
                env["RA_STUB_EXIT"] = str(stub_exit)
        elif session is not None:
            env["REMOTE_AGENTS_SESSION_ID"] = session
        for name, value in (overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return subprocess.run(
            [sys.executable, str(self.script), argv[0], "--root", str(root), *argv[1:]],
            capture_output=True,
            text=True,
            env=env,
            cwd=root if cwd is None else cwd,
            timeout=60,
            check=False,
        )


@pytest.fixture(params=sorted(_WRITERS))
def harness(request, tmp_path: Path) -> Harness:
    return _harness(_WRITERS[request.param], tmp_path)


@pytest.fixture
def codex_harness(tmp_path: Path) -> Harness:
    return _harness(_WRITERS["codex"], tmp_path)


@pytest.fixture
def staged_harness(tmp_path: Path) -> Harness:
    return _harness(_WRITERS["staged-planning"], tmp_path)


@pytest.fixture(params=sorted(name for name, writer in _WRITERS.items() if writer.legacy))
def legacy_harness(request, tmp_path: Path) -> Harness:
    return _harness(_WRITERS[request.param], tmp_path)


def _harness(writer: Writer, tmp_path: Path) -> Harness:
    checkout = os.environ.get(writer.variable, "")
    if not checkout:
        pytest.skip(f"{writer.variable} is unset: no checkout to run the {writer.name} writer")
    script = Path(checkout) / writer.relative
    assert script.is_file(), f"{writer.variable} is set but {script} does not exist"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    decoy_dir = tmp_path / "decoys"
    decoy_dir.mkdir()
    decoy = decoy_dir / "remote-agents"
    decoy.write_text(_DECOY)
    decoy.chmod(0o755)
    harness = Harness(writer, script, stub_dir, decoy_dir)
    harness.stub.write_text(_STUB)
    harness.stub.chmod(0o755)
    return harness


def _git_repo(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    (root / "plan.md").write_text("# a plan\n")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _git_repo(tmp_path / "repo")


def _new_id(harness: Harness, root: Path) -> str:
    result = harness.run(root, "new-id")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _written(harness: Harness, root: Path) -> dict[str, tuple[str, str]]:
    """Run every writer subcommand once; return {kind: (handoff id, file name)}."""
    ready_id, failed_id = _new_id(harness, root), _new_id(harness, root)
    plan = str(root / "plan.md")
    for argv in (
        ("ready", "--handoff-id", ready_id, "--plan", plan),
        ("accept", "--handoff-id", ready_id, "--plan", plan),
        ("fail", "--handoff-id", failed_id, "--code", "branch-mismatch"),
    ):
        result = harness.run(root, *argv)
        assert result.returncode == 0, (argv, result.stdout, result.stderr)
        assert result.stdout.strip() != "unsupervised", (argv, "the writer found no session")
    return {
        "ready": (ready_id, f"{ready_id}.ready.json"),
        "accepted": (ready_id, f"{ready_id}.accepted.json"),
        "failed": (failed_id, f"{failed_id}.failed.json"),
    }


def test_what_the_planning_side_writes_reads_back_typed(harness: Harness, repo: Path) -> None:
    written = _written(harness, repo)
    handoffs = repo / ".claude" / "handoffs"
    # The writer's own bookkeeping is there, and must not read as events.
    assert (handoffs / f".{written['accepted'][0]}.claim").exists()
    assert (handoffs / f".{written['failed'][0]}.claim").exists()
    assert (handoffs / ".gitignore").read_bytes() == b"*\n"

    events = {e.event: e for e in FileHandoffEnvelopes().events(repo)}

    assert set(events) == {HandoffEvent.READY, HandoffEvent.ACCEPTED, HandoffEvent.FAILED}
    ready, accepted, failed = (
        events[HandoffEvent.READY],
        events[HandoffEvent.ACCEPTED],
        events[HandoffEvent.FAILED],
    )
    assert ready.handoff_id == accepted.handoff_id == written["ready"][0]
    assert failed.handoff_id == written["failed"][0]
    # For the Codex writer, the shell carried DAEMON_STARTER all along: only the verb names it.
    assert {e.managed_session_id for e in events.values()} == {SESSION}
    assert ready.plan == accepted.plan == str(repo / "plan.md")
    assert failed.plan is None
    assert failed.failure_code == "branch-mismatch"
    assert ready.failure_code is None and accepted.failure_code is None
    assert all(e.timestamp.tzinfo is not None for e in events.values())


_TAMPERS = {
    "version-2": lambda body: body | {"version": 2},
    "protocol": lambda body: body | {"protocol": "planning-handoff-v2"},
    "legacy-protocol": lambda body: body | {"protocol": "remote-agents-handoff-v2"},
    "extra-key": lambda body: body | {"note": "hello"},
    "session-id": lambda body: body | {"managed_session_id": "../escape"},
    "traversal-id": lambda body: body | {"handoff_id": "h-../../x"},
}


@pytest.mark.parametrize("kind", ["ready", "accepted", "failed"])
@pytest.mark.parametrize("tamper", sorted(_TAMPERS))
def test_a_tampered_copy_of_each_kind_is_rejected_beside_the_real_one(
    harness: Harness, repo: Path, kind: str, tamper: str
) -> None:
    written = _written(harness, repo)
    handoffs = repo / ".claude" / "handoffs"
    original = json.loads((handoffs / written[kind][1]).read_bytes())
    copy_id = _new_id(harness, repo)
    body = _TAMPERS[tamper](original | {"handoff_id": copy_id})
    (handoffs / f"{copy_id}.{kind}.json").write_text(json.dumps(body))
    # An untampered copy under a name that is not its id: the name is part of the envelope.
    renamed_id = _new_id(harness, repo)
    (handoffs / f"{renamed_id}.{kind}.json").write_bytes((handoffs / written[kind][1]).read_bytes())

    ids = {(e.event, e.handoff_id) for e in FileHandoffEnvelopes().events(repo)}

    assert ids == {
        (HandoffEvent.READY, written["ready"][0]),
        (HandoffEvent.ACCEPTED, written["ready"][0]),
        (HandoffEvent.FAILED, written["failed"][0]),
    }


def _restamp_legacy(repo: Path) -> None:
    """Rewrite `request.json` as the installed supervisor (0.66.0) writes it: the legacy id."""
    request = repo / ".claude" / "handoffs" / "request.json"
    body = json.loads(request.read_bytes())
    request.write_text(json.dumps(body | {"protocol": LEGACY_PROTOCOL}))


def test_a_request_this_side_writes_is_one_the_planning_side_sees(
    harness: Harness, repo: Path
) -> None:
    """A legacy writer does not see a request this side now writes (`planning-handoff`): the
    interim, asserted rather than skipped. It still sees one in the legacy form the installed
    supervisor writes, and the installed supervisor is not upgraded until the cutover replaces
    it and these writers together."""
    adapter = FileHandoffEnvelopes()

    assert harness.run(repo, "request-pending").returncode == 1
    assert adapter.write_request(repo, SESSION) is True
    if harness.writer.legacy:
        assert harness.run(repo, "request-pending").returncode == 1
        _restamp_legacy(repo)
    assert harness.run(repo, "request-pending").returncode == 0
    assert harness.run(repo, "request-pending", session="another-session").returncode == 1

    adapter.clear_request(repo, SESSION)
    assert harness.run(repo, "request-pending").returncode == 1


def test_a_legacy_writer_does_not_see_a_request_this_branch_writes(
    legacy_harness: Harness, repo: Path
) -> None:
    """The interim, asserted rather than skipped: this branch stamps `planning-handoff`, which
    the coder-plugins and Codex writers do not read. Nothing breaks, because the installed
    supervisor is not upgraded until the cutover (staged-planning master Sub-plan 7) replaces
    it and these two writers together."""
    assert FileHandoffEnvelopes().write_request(repo, SESSION) is True
    assert (
        json.loads((repo / ".claude" / "handoffs" / "request.json").read_bytes())["protocol"]
        == PROTOCOL
    )

    assert legacy_harness.run(repo, "request-pending").returncode == 1


def test_a_codex_writer_stamps_the_verbs_session_never_the_inherited_one(
    codex_harness: Harness, repo: Path
) -> None:
    """An unmanaged shell (the verb exits 3) writes nothing at all (DEC-128). Codex only among
    the legacy writers: the claude writer names its session by the inherited variable, by
    design."""
    harness = codex_harness
    handoff = _new_id(harness, repo)
    result = harness.run(
        repo, "ready", "--handoff-id", handoff, "--plan", str(repo / "plan.md"), stub_exit=3
    )
    assert (result.returncode, result.stdout.strip()) == (0, "unsupervised")
    assert not (repo / ".claude" / "handoffs" / f"{handoff}.ready.json").exists()


def test_the_planning_side_keeps_the_gitignore_this_side_wrote(
    harness: Harness, repo: Path
) -> None:
    adapter = FileHandoffEnvelopes()
    assert adapter.write_request(repo, SESSION) is True
    gitignore = repo / ".claude" / "handoffs" / ".gitignore"
    stamp = gitignore.stat().st_mtime_ns

    _written(harness, repo)

    # Its writer rewrites a .gitignore that is not exactly its own; an untouched one agrees.
    assert gitignore.read_bytes() == b"*\n"
    assert gitignore.stat().st_mtime_ns == stamp
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert ".claude" not in status.stdout


# --- The staged-planning writer: the neutral protocol, named by its command ---


def test_every_envelope_the_staged_planning_writer_writes_carries_the_neutral_protocol(
    staged_harness: Harness, repo: Path
) -> None:
    written = _written(staged_harness, repo)
    handoffs = repo / ".claude" / "handoffs"

    protocols = {
        kind: json.loads((handoffs / name).read_bytes())["protocol"]
        for kind, (_, name) in written.items()
    }

    assert protocols == {"ready": PROTOCOL, "accepted": PROTOCOL, "failed": PROTOCOL}
    assert PROTOCOL == "planning-handoff"


def test_the_staged_planning_writer_without_its_command_is_unsupervised_and_writes_nothing(
    staged_harness: Harness, repo: Path
) -> None:
    """No variable is ever read as the id itself, so a session variable alone names nothing."""
    handoff = _new_id(staged_harness, repo)

    result = staged_harness.run(
        repo,
        "ready",
        "--handoff-id",
        handoff,
        "--plan",
        str(repo / "plan.md"),
        overrides={_ID_COMMAND: None, "REMOTE_AGENTS_SESSION_ID": SESSION},
    )

    assert (result.returncode, result.stdout.strip()) == (0, "unsupervised"), result.stderr
    assert not (repo / ".claude").exists()


def test_the_staged_planning_writer_writes_nothing_when_its_command_exits_3(
    staged_harness: Harness, repo: Path
) -> None:
    handoff = _new_id(staged_harness, repo)

    result = staged_harness.run(
        repo, "ready", "--handoff-id", handoff, "--plan", str(repo / "plan.md"), stub_exit=3
    )

    assert (result.returncode, result.stdout.strip()) == (0, "unsupervised")
    assert not (repo / ".claude").exists()
    # The stub, not the decoy, was the one asked.
    assert staged_harness.seen.read_text().splitlines()[1] == "argv session-id"


def test_a_request_for_the_daemon_starter_is_not_one_for_a_staged_planning_shell(
    staged_harness: Harness, repo: Path
) -> None:
    """The shell carries the daemon starter's variable; the writer names its session by its
    command, so a request naming the starter is not one for it (DEC-128). The request carries
    the id this writer reads, so the session and not the protocol is what refuses it."""
    assert FileHandoffEnvelopes().write_request(repo, DAEMON_STARTER) is True

    assert staged_harness.run(repo, "request-pending").returncode == 1
    assert staged_harness.run(repo, "request-pending", session=DAEMON_STARTER).returncode == 0


# --- Root direction: the writer reports the root it writes under ---


def _seen_calls(harness: Harness) -> list[tuple[str, str]]:
    lines = harness.seen.read_text().splitlines()
    assert len(lines) % 2 == 0, lines
    return [
        (root.removeprefix("root "), argv.removeprefix("argv "))
        for root, argv in zip(lines[::2], lines[1::2], strict=True)
    ]


def test_staged_planning_reported_root_reaches_the_command_with_nothing_appended(
    staged_harness: Harness, tmp_path: Path
) -> None:
    """Run from a folder in repo A with `--root` a link to repo B: every identity call
    (`ready`, `accept`, `fail`, `request-pending`) gives the command B, resolved, as
    `PLANNING_HANDOFF_ROOT`, and its argv as configured, so an older verb that refuses an
    extra argument still answers."""
    folder_a = _git_repo(tmp_path / "a") / "work"
    folder_a.mkdir()
    repo_b = _git_repo(tmp_path / "b")
    resolved_b = os.path.realpath(repo_b)
    link_b = tmp_path / "link-b"
    link_b.symlink_to(repo_b)
    plan = str(repo_b / "plan.md")
    ready_id, failed_id = (_new_id(staged_harness, repo_b) for _ in range(2))
    assert FileHandoffEnvelopes().write_request(repo_b, SESSION) is True

    for argv in (
        ("ready", "--handoff-id", ready_id, "--plan", plan),
        ("accept", "--handoff-id", ready_id, "--plan", plan),
        ("fail", "--handoff-id", failed_id, "--code", "branch-mismatch"),
    ):
        result = staged_harness.run(link_b, *argv, cwd=folder_a)
        assert result.returncode == 0, (argv, result.stdout, result.stderr)
        assert result.stdout.strip() != "unsupervised", (argv, result.stderr)
    assert staged_harness.run(link_b, "request-pending", cwd=folder_a).returncode == 0

    assert _seen_calls(staged_harness) == [(resolved_b, "session-id")] * 4
    assert not (tmp_path / "a" / ".claude").exists()
    events = {e.event for e in FileHandoffEnvelopes().events(repo_b)}
    assert events == {HandoffEvent.READY, HandoffEvent.ACCEPTED, HandoffEvent.FAILED}


def test_staged_planning_reported_root_is_recorded_by_the_real_verb(
    staged_harness: Harness, tmp_path: Path
) -> None:
    """With RA's own identity command, a temp `HOME` and a managed Claude-shaped session
    (named by its pane's variable, no Codex thread, no roster), `ready --root B` from repo A
    records B for that session where the service reads it."""
    home = tmp_path / "home"
    home.mkdir()
    activity = ProductionPaths.for_home(home).activity_directory
    activity.mkdir(parents=True, mode=0o700)
    session = str(SessionId.new())
    folder_a = _git_repo(tmp_path / "a")
    repo_b = _git_repo(tmp_path / "b")
    handoff = _new_id(staged_harness, repo_b)

    result = staged_harness.run(
        repo_b,
        "ready",
        "--handoff-id",
        handoff,
        "--plan",
        str(repo_b / "plan.md"),
        cwd=folder_a,
        overrides={
            "HOME": str(home),
            _ID_COMMAND: supervisor_id_command(),
            "REMOTE_AGENTS_SESSION_ID": session,
            "CODEX_THREAD_ID": None,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != "unsupervised", result.stderr
    (ready,) = FileHandoffEnvelopes().events(repo_b)
    assert ready.managed_session_id == session
    roots = FileReportedRoots(activity, accept=acceptable_root)
    assert roots.latest(session) == Path(os.path.realpath(repo_b))


def test_a_legacy_writer_reports_no_reported_root(legacy_harness: Harness, tmp_path: Path) -> None:
    """The interim, asserted rather than skipped: the coder-plugins and Codex writers report no
    root, so a cross-repo session is resumed by hand until the cutover (staged-planning master
    Sub-plan 7) replaces the installed supervisor and these writers together. Each still writes
    its envelope under B; nothing is recorded, even with RA's own command set beside it."""
    home = tmp_path / "home"
    home.mkdir()
    activity = ProductionPaths.for_home(home).activity_directory
    activity.mkdir(parents=True, mode=0o700)
    folder_a = _git_repo(tmp_path / "a")
    repo_b = _git_repo(tmp_path / "b")
    handoff = _new_id(legacy_harness, repo_b)

    result = legacy_harness.run(
        repo_b,
        "ready",
        "--handoff-id",
        handoff,
        "--plan",
        str(repo_b / "plan.md"),
        cwd=folder_a,
        overrides={
            "HOME": str(home),
            _ID_COMMAND: supervisor_id_command(),
            "CODEX_THREAD_ID": None,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != "unsupervised", result.stderr
    (ready,) = FileHandoffEnvelopes().events(repo_b)
    assert ready.managed_session_id == SESSION
    roots = FileReportedRoots(activity, accept=acceptable_root)
    assert roots.sessions() == ()
    assert roots.latest(SESSION) is None
    if legacy_harness.writer.asks_the_verb:
        assert _seen_calls(legacy_harness) == [("-", "session-id")]
