"""The identity command a launch writes is answered by this install, not by whatever is on `PATH`.

A pane's `PATH` leads with agent folders and the owner's `~/.local/bin`, which can hold an older
installed `remote-agents`. The handoff writer splits `PLANNING_SUPERVISOR_ID_CMD` with `shlex` and
runs it with no shell (DEC-128), so the command must name the running interpreter by absolute
path. This runs the real command from a real launch profile with a decoy earlier on `PATH`.

The writer runs it from the checkout it works in, which the agent controls. `python -m` would
put that folder first on `sys.path`, so the command runs the interpreter isolated (`-I`): a
`remote_agents` package or a module named like the standard library's in the checkout neither
answers for this install nor breaks it.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.session_identity import SUPERVISOR_ID_COMMAND_VARIABLE


def _ask(tmp_path: Path, cwd: Path) -> tuple[subprocess.CompletedProcess, SessionId, Path]:
    """Run a real launch profile's identity command from `cwd`, a decoy first on `PATH`."""
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
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed, session_id, decoy


def test_a_decoy_remote_agents_earlier_on_path_does_not_answer(tmp_path: Path) -> None:
    completed, session_id, decoy = _ask(tmp_path, tmp_path)

    # The decoy was a real contender: a bare name would have run it.
    assert shutil.which("remote-agents", path=f"{decoy.parent}:/usr/bin:/bin") == str(decoy)
    assert completed.stdout == f"{session_id}\n", completed.stderr
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("remote_agents/__main__.py", 'print("ra-shadow_0")\n'),
        ("json.py", 'raise SystemExit("a checkout module shadowed json")\n'),
    ],
    ids=["package", "stdlib-name"],
)
def test_a_module_in_the_writers_folder_neither_answers_nor_breaks_it(
    tmp_path: Path, name: str, body: str
) -> None:
    checkout = tmp_path / "checkout"
    shadow = checkout / name
    shadow.parent.mkdir(parents=True)
    (shadow.parent / "__init__.py").touch()
    shadow.write_text(body, encoding="utf-8")

    completed, session_id, _ = _ask(tmp_path, checkout)

    assert completed.stdout == f"{session_id}\n", completed.stderr
    assert completed.returncode == 0, completed.stderr
