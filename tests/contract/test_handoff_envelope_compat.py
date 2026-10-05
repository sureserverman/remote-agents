"""The envelope reader agrees with the planning plugin's own writer, run for real.

The protocol has two implementations in two repositories: coder-plugins'
`handoff-envelope.py` writes the envelopes and reads `request.json`, and
`adapters/workflow` does the reverse. Fixtures written by hand here would only prove the
reader agrees with this file's idea of the protocol; this runs the other side's script into
a temporary git repository and reads what it actually wrote. It also writes a tampered copy
of every envelope kind beside the real one, so a reader that accepted anything shaped like
JSON would fail here and not just in the security suite.

Skipped unless `RA_CODER_PLUGINS` names a coder-plugins checkout, so CI -- which has none --
is unaffected. Set but pointing at no script is a failure, not a skip: a gate run with the
variable set is claiming this test ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.workflow.handoff_envelopes import FileHandoffEnvelopes
from remote_agents.ports.handoff_envelopes import HandoffEvent

_CHECKOUT = os.environ.get("RA_CODER_PLUGINS", "")
_SCRIPT = Path(_CHECKOUT) / "planning/skills/executing-plans/scripts/handoff-envelope.py"
SESSION = "ra-compat_1"

pytestmark = pytest.mark.skipif(
    not _CHECKOUT, reason="RA_CODER_PLUGINS is unset: no coder-plugins checkout to run against"
)


def _run(root: Path, *argv: str, session: str | None = SESSION) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "REMOTE_AGENTS_SESSION_ID"}
    if session is not None:
        env["REMOTE_AGENTS_SESSION_ID"] = session
    return subprocess.run(
        [sys.executable, str(_SCRIPT), argv[0], "--root", str(root), *argv[1:]],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    assert _SCRIPT.is_file(), f"RA_CODER_PLUGINS is set but {_SCRIPT} does not exist"
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    (root / "plan.md").write_text("# a plan\n")
    return root


def _new_id(root: Path) -> str:
    result = _run(root, "new-id")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _written(root: Path) -> dict[str, tuple[str, str]]:
    """Run every writer subcommand once; return {kind: (handoff id, file name)}."""
    ready_id, failed_id = _new_id(root), _new_id(root)
    plan = str(root / "plan.md")
    for argv in (
        ("ready", "--handoff-id", ready_id, "--plan", plan),
        ("accept", "--handoff-id", ready_id, "--plan", plan),
        ("fail", "--handoff-id", failed_id, "--code", "branch-mismatch"),
    ):
        result = _run(root, *argv)
        assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return {
        "ready": (ready_id, f"{ready_id}.ready.json"),
        "accepted": (ready_id, f"{ready_id}.accepted.json"),
        "failed": (failed_id, f"{failed_id}.failed.json"),
    }


def test_what_the_planning_side_writes_reads_back_typed(repo: Path) -> None:
    written = _written(repo)
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
    repo: Path, kind: str, tamper: str
) -> None:
    written = _written(repo)
    handoffs = repo / ".claude" / "handoffs"
    original = json.loads((handoffs / written[kind][1]).read_bytes())
    copy_id = _new_id(repo)
    body = _TAMPERS[tamper](original | {"handoff_id": copy_id})
    (handoffs / f"{copy_id}.{kind}.json").write_text(json.dumps(body))
    # An untampered copy under a name that is not its id: the name is part of the envelope.
    renamed_id = _new_id(repo)
    (handoffs / f"{renamed_id}.{kind}.json").write_bytes((handoffs / written[kind][1]).read_bytes())

    ids = {(e.event, e.handoff_id) for e in FileHandoffEnvelopes().events(repo)}

    assert ids == {
        (HandoffEvent.READY, written["ready"][0]),
        (HandoffEvent.ACCEPTED, written["ready"][0]),
        (HandoffEvent.FAILED, written["failed"][0]),
    }


def test_a_request_this_side_writes_is_one_the_planning_side_sees(repo: Path) -> None:
    adapter = FileHandoffEnvelopes()

    assert _run(repo, "request-pending").returncode == 1
    assert adapter.write_request(repo, SESSION) is True
    assert _run(repo, "request-pending").returncode == 0
    assert _run(repo, "request-pending", session="another-session").returncode == 1

    adapter.clear_request(repo, SESSION)
    assert _run(repo, "request-pending").returncode == 1


def test_the_planning_side_keeps_the_gitignore_this_side_wrote(repo: Path) -> None:
    adapter = FileHandoffEnvelopes()
    assert adapter.write_request(repo, SESSION) is True
    gitignore = repo / ".claude" / "handoffs" / ".gitignore"
    stamp = gitignore.stat().st_mtime_ns

    _written(repo)

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
