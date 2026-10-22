"""The envelope reader agrees with each planning plugin's own writer, run for real.

The protocol's writer has two implementations, one per host that rolls over: coder-plugins'
`handoff-envelope.py` for Claude Code, and engineering-skills' Codex fork of it. Each writes
the envelopes and reads `request.json`; `adapters/workflow` does the reverse. Fixtures written
by hand here would only prove the reader agrees with this file's idea of the protocol; this
runs the other side's script into a temporary git repository and reads what it actually wrote.
It also writes a tampered copy of every envelope kind beside the real one, so a reader that
accepted anything shaped like JSON would fail here and not just in the security suite.

Each writer is a parameter. `claude` runs coder-plugins' script, naming its session by
`REMOTE_AGENTS_SESSION_ID`. `codex` runs engineering-skills' fork, which names its session by
asking `remote-agents session-id` (DEC-128): a stub of that command on `PATH` prints the id, and
the shell carries a different `REMOTE_AGENTS_SESSION_ID` throughout -- the daemon starter's, as
measured -- which the fork must never stamp.

A writer is skipped unless its variable names a checkout (`RA_CODER_PLUGINS`,
`RA_ENGINEERING_SKILLS`), so CI -- which has neither -- is unaffected. Set but pointing at no
script is a failure, not a skip: a gate run with the variable set is claiming this test ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from remote_agents.adapters.workflow.handoff_envelopes import (
    LEGACY_PROTOCOL,
    FileHandoffEnvelopes,
)
from remote_agents.ports.handoff_envelopes import HandoffEvent

SESSION = "ra-compat_1"
#: What a Codex tool shell inherits from the pane that started the shared daemon (DEC-128).
DAEMON_STARTER = "ra-starter_9"

#: The `remote-agents` a Codex writer finds on PATH: it prints the session it is told to, or
#: exits as told. The real verb's rule is tested in this repo; here only the writer's use of it.
_STUB = """#!/bin/sh
if [ -n "$RA_STUB_EXIT" ]; then exit "$RA_STUB_EXIT"; fi
if [ "$1" != "session-id" ] || [ "$#" -ne 1 ]; then exit 2; fi
printf '%s\n' "$RA_STUB_SESSION"
"""


@dataclass(frozen=True)
class Writer:
    name: str
    variable: str
    relative: str
    asks_the_verb: bool
    #: Reads only `remote-agents-handoff`. Both writers here do, until the staged-planning
    #: cutover (master Sub-plan 7) retires them.
    legacy: bool = True


_WRITERS = {
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

    def run(
        self, root: Path, *argv: str, session: str | None = SESSION, stub_exit: int | None = None
    ) -> subprocess.CompletedProcess:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"REMOTE_AGENTS_SESSION_ID", "RA_STUB_SESSION", "RA_STUB_EXIT"}
        }
        if self.writer.asks_the_verb:
            env["PATH"] = f"{self.stub_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
            env["REMOTE_AGENTS_SESSION_ID"] = DAEMON_STARTER
            if session is None:
                env["RA_STUB_EXIT"] = "3"
            else:
                env["RA_STUB_SESSION"] = session
            if stub_exit is not None:
                env["RA_STUB_EXIT"] = str(stub_exit)
        elif session is not None:
            env["REMOTE_AGENTS_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(self.script), argv[0], "--root", str(root), *argv[1:]],
            capture_output=True,
            text=True,
            env=env,
            cwd=root,
            timeout=60,
            check=False,
        )


@pytest.fixture(params=sorted(_WRITERS))
def harness(request, tmp_path: Path) -> Harness:
    return _harness(_WRITERS[request.param], tmp_path)


@pytest.fixture
def codex_harness(tmp_path: Path) -> Harness:
    return _harness(_WRITERS["codex"], tmp_path)


def _harness(writer: Writer, tmp_path: Path) -> Harness:
    checkout = os.environ.get(writer.variable, "")
    if not checkout:
        pytest.skip(f"{writer.variable} is unset: no checkout to run the {writer.name} writer")
    script = Path(checkout) / writer.relative
    assert script.is_file(), f"{writer.variable} is set but {script} does not exist"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "remote-agents"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    return Harness(writer, script, stub_dir)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    (root / "plan.md").write_text("# a plan\n")
    return root


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
    "protocol": lambda body: body | {"protocol": "remote-agents-handoff-v2"},
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


def test_a_codex_writer_stamps_the_verbs_session_never_the_inherited_one(
    codex_harness: Harness, repo: Path
) -> None:
    """A request for the daemon starter is not one for this shell, and an unmanaged shell
    (the verb exits 3) writes nothing at all (DEC-128). Codex only: the claude writer names its
    session by the inherited variable, by design. The request carries the legacy id, the one
    this fork reads, so the session and not the protocol is what refuses it."""
    harness = codex_harness
    adapter = FileHandoffEnvelopes()
    assert adapter.write_request(repo, DAEMON_STARTER) is True
    _restamp_legacy(repo)
    assert harness.run(repo, "request-pending").returncode == 1

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
