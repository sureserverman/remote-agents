"""Opt-in proof that a real claude session spools activity, and only a managed one does.

Everything else about this feature is checked against fixtures and fake payloads. This is the
one place a real `claude` runs with a real hook installed, because the two claims that matter
most cannot be established any other way: that the hook fires at all against the agent as
shipped, and that a session the service did not start stays silent. The second is a negative
about a process the service never sees, so it has to be produced rather than swept for.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required, as it is for every file here. The hook is
installed into a settings file made for the test and passed with `--settings`, and it writes
to a spool made for the test and passed with `--activity-dir`; neither the operator's
`~/.claude/settings.json` nor their real spool is read or written.

`HOME` is deliberately *not* isolated. An earlier version pointed it at a temporary directory
for tidiness, and the drill immediately reported that a managed session produced `SessionEnd`
and no `Stop` -- because a `claude` with no credentials exits at the login prompt rather than
taking a turn. That is the isolation, not the agent, and a drill whose whole purpose is to
observe a real turn has to let the agent actually take one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.agents.hook_install import install_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.domain.models import SessionId
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

_TURN = "Reply with exactly the word: spooled"


def _requirements(tmp_path: Path) -> tuple[Path, Path]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    if shutil.which("claude") is None:
        pytest.skip("BLOCKED: executable_missing")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "sonnet"}, indent=2) + "\n", encoding="utf-8")
    spool = tmp_path / "activity"
    install_agent_hooks(settings, executable=Path(sys.executable), activity_directory=spool)
    return settings, spool


def _run_claude(settings: Path, workspace: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        ["claude", "-p", _TURN, "--settings", str(settings)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=workspace,
        env={**os.environ, **environment},
    )
    if "Please run /login" in f"{completed.stdout}{completed.stderr}":
        pytest.skip("BLOCKED: claude is not logged in")
    return completed.stdout


@pytest.mark.live_profile
def test_a_managed_claude_session_spools_its_own_stop(tmp_path: Path) -> None:
    settings, spool = _requirements(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = SessionId.new()

    _run_claude(settings, workspace, {SESSION_ID_VARIABLE: str(session_id)})

    activities = drain_activity(spool)
    assert activities, "a managed session's Stop hook spooled nothing"
    assert {activity.session_id for activity in activities} == {str(session_id)}
    assert ActivityKind.COMPLETED in {activity.kind for activity in activities}


@pytest.mark.live_profile
def test_a_session_this_service_did_not_start_spools_nothing(tmp_path: Path) -> None:
    """The guard, against the real agent rather than against a fake payload."""
    settings, spool = _requirements(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _run_claude(settings, workspace, {SESSION_ID_VARIABLE: ""})

    assert drain_activity(spool) == ()
    assert not spool.exists() or list(spool.iterdir()) == []
