from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from remote_agents.application.activity import drain_activity
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE


def _run(directory: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "remote_agents",
            "agent-event",
            "--provider",
            "codex",
            "--activity-dir",
            str(directory),
        ],
        input=b'{"hook_event_name":"PermissionRequest","tool_input":{"command":"secret"}}',
        capture_output=True,
        env=environment,
        check=False,
    )


def test_codex_hook_cli_spools_only_managed_sessions(tmp_path: Path) -> None:
    spool = tmp_path / "activity"
    spool.mkdir(mode=0o700)
    environment = {**os.environ, SESSION_ID_VARIABLE: "managed-codex"}

    completed = _run(spool, environment)
    assert completed.returncode == 0 and completed.stdout == b""
    (activity,) = drain_activity(spool)
    assert activity.kind is ActivityKind.NEEDS_ANSWER and activity.detail is None

    unmanaged = {key: value for key, value in environment.items() if key != SESSION_ID_VARIABLE}
    assert _run(spool, unmanaged).returncode == 0
    assert drain_activity(spool) == ()
