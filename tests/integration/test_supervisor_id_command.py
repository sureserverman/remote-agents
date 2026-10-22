"""The identity command a launch writes is answered by this install, not by whatever is on `PATH`.

A pane's `PATH` leads with agent folders and the owner's `~/.local/bin`, which can hold an older
installed `remote-agents`. The handoff writer splits `PLANNING_SUPERVISOR_ID_CMD` with `shlex` and
runs it with no shell (DEC-128), so the command must name the running interpreter by absolute
path. This runs the real command from a real launch profile with a decoy earlier on `PATH`.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.session_identity import SUPERVISOR_ID_COMMAND_VARIABLE


def test_a_decoy_remote_agents_earlier_on_path_does_not_answer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    decoys = tmp_path / "decoys"
    decoys.mkdir()
    decoy = decoys / "remote-agents"
    decoy.write_text("#!/bin/sh\necho ra-decoy_0\n", encoding="utf-8")
    decoy.chmod(0o755)
    claude = next(d for d in closed_profiles() if str(d.profile_id) == "claude")
    session_id = SessionId.new()
    curated = {"HOME": str(home), "LANG": "C.UTF-8", "PATH": f"{decoys}:/usr/bin:/bin"}

    environment = build_launch_profile(
        claude, Path("/usr/bin/claude"), session_id, curated
    ).environment
    completed = subprocess.run(
        shlex.split(environment[SUPERVISOR_ID_COMMAND_VARIABLE]),
        env=environment,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.stdout == f"{session_id}\n", completed.stderr
    assert completed.returncode == 0, completed.stderr
