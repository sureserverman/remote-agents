from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from remote_agents.adapters.agents.hook_install import install_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.bootstrap import main
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE


def _run(command: str, environment: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        shlex.split(command),
        input=b'{"hook_event_name":"PermissionRequest","tool_input":{"command":"secret"}}',
        capture_output=True,
        env=environment,
        check=False,
    )


def test_codex_hook_cli_spools_only_managed_sessions(tmp_path: Path) -> None:
    spool = tmp_path / "activity"
    spool.mkdir(mode=0o700)
    hooks = tmp_path / "hooks.json"
    hooks.write_text("{}\n", encoding="utf-8")
    install_agent_hooks(
        hooks, executable=Path(sys.executable), activity_directory=spool, provider="codex"
    )
    command = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["PermissionRequest"][0][
        "hooks"
    ][0]["command"]
    environment = {**os.environ, SESSION_ID_VARIABLE: "managed-codex"}

    completed = _run(command, environment)
    assert completed.returncode == 0 and completed.stdout == b""
    (activity,) = drain_activity(spool)
    assert activity.kind is ActivityKind.NEEDS_ANSWER and activity.detail is None

    unmanaged = {key: value for key, value in environment.items() if key != SESSION_ID_VARIABLE}
    assert _run(command, unmanaged).returncode == 0
    assert drain_activity(spool) == ()

    assert main(["agent-event", "--provider", "codex", "--activity-dir", str(spool)]) == 0
