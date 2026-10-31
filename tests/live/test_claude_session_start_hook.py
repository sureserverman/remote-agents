"""What `SessionStart` does on the Claude this host runs, measured rather than read.

The readiness signal (DEC-135) rests on three facts the hooks reference does not state, so they
are pinned here per Claude version. No prompt is typed, so these spend no quota.

Measured on Claude Code 2.1.314, 2026-10-30:

- The payload carries `hook_event_name` and `session_id` (and `cwd`, `model`, `source`,
  `scratchpad_dir`, `transcript_path`).
- It fired 0.8 s after launch, with thirty wildcard allow rules holding the banner off an 80x24
  screen for the whole 22 s watched: the case the screen marker cannot see.
- In a folder never trusted it did **not** fire while the dialog stood: 25 s, no record. So the
  hook cannot promote a session stopped on that question, on this version. `TmuxTerminal` still
  reads the trust screen first, because that order is what holds if a later version moves it.

`/tmp` is a trusted folder on this host, so the first two cases get no dialog there. The third
works under `/var/tmp`, which is not. A host that trusts `/var/tmp` skips it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_SOCKET = "remote-agents-test-session-start"
_DIALOG = "Accessing workspace:"
_BUDGET_SECONDS = 20.0
_WATCH_SECONDS = 12.0
_DUMP = """
import json, sys
document = json.load(sys.stdin)
with open(sys.argv[1], "a") as out:
    out.write(json.dumps(document) + "\\n")
"""


def _tmux(*arguments: str) -> str:
    return subprocess.run(
        ["tmux", "-L", _SOCKET, *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout


@pytest.fixture
def server() -> Iterator[None]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    for needed in ("claude", "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    _tmux("kill-server")
    yield
    _tmux("kill-server")


def warning_rules(count: int = 30) -> list[str]:
    """Allow rules Claude warns about, one wrapped warning each, enough to fill a screen."""
    return [f"Bash(rm -rf /tmp/ra-probe-{i}*.deb /tmp/ra-probe-{i}-stock)" for i in range(count)]


def _launch(workspace: Path, tmp_path: Path) -> Path:
    """Start claude at 80x24 with a payload-dumping `SessionStart`; answer where it writes."""
    dump = tmp_path / "dump.py"
    dump.write_text(_DUMP)
    records = tmp_path / "session-start.jsonl"
    settings = tmp_path / "settings.json"
    command = f"{sys.executable} -I {dump} {records}"
    settings.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
        )
    )
    _tmux(
        "new-session", "-d", "-x", "80", "-y", "24", "-c", str(workspace),
        "claude", "--settings", str(settings),
    )  # fmt: skip
    return records


@pytest.mark.live_profile
def test_it_fires_inside_the_budget_with_the_banner_off_the_screen(
    server: None, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": warning_rules()}})
    )

    records = _launch(workspace, tmp_path)

    started = time.monotonic()
    fired_after: float | None = None
    banner_seen = False
    while time.monotonic() - started < _BUDGET_SECONDS:
        banner_seen = banner_seen or "Claude Code" in _tmux("capture-pane", "-p")
        if fired_after is None and records.is_file():
            fired_after = time.monotonic() - started
        time.sleep(0.25)

    assert fired_after is not None, "SessionStart did not fire inside the startup budget"
    assert not banner_seen, "the banner reached the screen, so this case proves nothing"
    document = json.loads(records.read_text().splitlines()[0])
    assert document["hook_event_name"] == "SessionStart"
    assert document["session_id"]


@pytest.mark.live_profile
def test_it_fires_on_a_plain_launch_too(server: None, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    records = _launch(workspace, tmp_path)

    deadline = time.monotonic() + _BUDGET_SECONDS
    while not records.is_file() and time.monotonic() < deadline:
        time.sleep(0.25)
    assert records.is_file()


@pytest.mark.live_profile
def test_it_does_not_fire_while_the_trust_dialog_stands(server: None, tmp_path: Path) -> None:
    workspace = Path(tempfile.mkdtemp(prefix="ra-session-start-", dir="/var/tmp"))
    try:
        records = _launch(workspace, tmp_path)

        deadline = time.monotonic() + _WATCH_SECONDS
        dialog_seen = False
        while time.monotonic() < deadline:
            dialog_seen = dialog_seen or _DIALOG in _tmux("capture-pane", "-p")
            time.sleep(0.25)

        if not dialog_seen:
            pytest.skip("BLOCKED: this host trusts /var/tmp, so no dialog stood")
        assert not records.exists(), "SessionStart fired before the folder was trusted"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
