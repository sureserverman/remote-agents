"""Live premise drill: can a `!` command the service types record a thread binding?

The binding plan has the service type one `!` command into its own pane. The tool shell's session
variable cannot name a session under the shared daemon, and the thread variable is the only fact
each shell holds for itself (DEC-128). So this measures what the design relies on, for the
launches the service really makes: every permission preset's own flags, on one daemon, with two
plain panes around them as the control.

Each preset's `!` shell writes outside its workspace, because the activity directory is outside
it. The drill also records whether the launch joined the daemon, what its footer warns, what `/new`
does to the thread, and whether a thread holding only a `!` command can be resumed. The result goes
to `RA_PREMISE_OUT`. Only the control is asserted here: the results are for reading.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required: it spends short Codex turns on the owner's account.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import PermissionPreset, closed_profiles

from .test_codex_tool_shell_identity import (
    _TOKEN_SCRIPT,
    Drill,
    _daemons,
    _footnote,
    _git,
    _open,
    _processes,
    _read_seen,
    _send,
    _tmux,
    drill,  # noqa: F401 - the shared fixture
)

pytestmark = pytest.mark.live_acceptance

_MARKER = "marker-of-first-plain-pane"
_TURN = "Run the shell command `sh token.sh seen-turn.txt`, then reply: done"


def _bang_script(outside: Path) -> str:
    return (
        f'{_TOKEN_SCRIPT}printf "%s" "${{CODEX_THREAD_ID-}}" > "{outside}" 2> outside-error.txt\n'
    )


def _workspace(drill: Drill, name: str) -> Path:  # noqa: F811
    workspace = drill.project.parent / name
    _git(drill.project, "worktree", "add", "-q", "-b", name, str(workspace))
    return workspace


def _screen(drill: Drill, pane: str) -> str:  # noqa: F811
    return _tmux(drill.socket, "capture-pane", "-p", "-t", pane).stdout


def _settled(drill: Drill, pane: str, seconds: float = 30.0) -> None:  # noqa: F811
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and "esc to interrupt" in _screen(drill, pane):
        time.sleep(1.0)


def _launch(
    drill: Drill,  # noqa: F811
    name: str,
    flags: tuple[str, ...],
    session: str | None = None,
) -> dict[str, object]:
    """One launch: a `!` command that writes outside its workspace, then one agent turn."""
    workspace = _workspace(drill, name)
    outside = drill.project.parent / f"outside-{name}"
    (workspace / "token.sh").write_text(_bang_script(outside), encoding="utf-8")
    pane = _open(drill.socket, name, workspace, drill.codex_home, session, (*drill.command, *flags))
    _send(drill.socket, pane, "!sh token.sh seen-bang.txt")
    bang = _read_seen(workspace / "seen-bang.txt", 30.0)
    _settled(drill, pane)
    error = workspace / "outside-error.txt"
    wrote = outside.is_file() and bool(outside.read_text(encoding="utf-8"))
    _send(drill.socket, pane, _TURN)
    turn = _read_seen(workspace / "seen-turn.txt", 180.0)
    screen = _footnote(drill.socket, pane)
    processes = _processes(drill.codex_home)
    return {
        "name": name,
        "flags": list(flags),
        "bang": bang,
        "bang_wrote": wrote,
        "bang_error": error.read_text(encoding="utf-8") if error.is_file() else None,
        "bang_thread": outside.read_text(encoding="utf-8") if wrote else None,
        "turn": turn,
        "turn_thread": None if turn is None else turn["thread"],
        "daemons": _daemons(processes),
        "marker_seen": None if turn is None else turn["session"] == _MARKER,
        "screen": screen,
    }


def _after_new(drill: Drill) -> dict[str, object]:  # noqa: F811
    """What `/new` does to the thread a `!` shell is told."""
    workspace = _workspace(drill, "after-new")
    (workspace / "token.sh").write_text(_TOKEN_SCRIPT, encoding="utf-8")
    pane = _open(drill.socket, "after-new", workspace, drill.codex_home, None, drill.command)
    _send(drill.socket, pane, "!sh token.sh before.txt")
    before = _read_seen(workspace / "before.txt", 30.0)
    _settled(drill, pane)
    _send(drill.socket, pane, "/new")
    time.sleep(3.0)
    _send(drill.socket, pane, "!sh token.sh after.txt")
    after = _read_seen(workspace / "after.txt", 30.0)
    return {"before": before, "after": after, "screen": _screen(drill, pane)}


def _resume_bang_only(drill: Drill) -> dict[str, object]:  # noqa: F811
    """Whether a thread that only ever ran a `!` command can be resumed, as a restart would."""
    workspace = _workspace(drill, "bang-only")
    (workspace / "token.sh").write_text(_TOKEN_SCRIPT, encoding="utf-8")
    pane = _open(drill.socket, "bang-only", workspace, drill.codex_home, None, drill.command)
    _send(drill.socket, pane, "!sh token.sh before.txt")
    before = _read_seen(workspace / "before.txt", 30.0)
    _settled(drill, pane)
    _send(drill.socket, pane, "/exit")
    time.sleep(3.0)
    rollouts = [
        path.name
        for path in (drill.codex_home / "sessions").glob("*/*/*/rollout-*.jsonl")
        if before is not None and before["thread"] in path.name
    ]
    if before is None:
        return {
            "before": None,
            "rollouts": rollouts,
            "resumed": None,
            "screen": _screen(drill, pane),
        }
    command = (*drill.command, "resume", before["thread"])
    try:
        again = _open(drill.socket, "bang-resume", workspace, drill.codex_home, None, command)
    except BaseException as error:  # noqa: BLE001 - the failure is the measurement
        return {"before": before, "rollouts": rollouts, "resumed": False, "error": repr(error)}
    _send(drill.socket, again, "!sh token.sh after.txt")
    after = _read_seen(workspace / "after.txt", 30.0)
    return {
        "before": before,
        "rollouts": rollouts,
        "resumed": after is not None and after["thread"] == before["thread"],
        "after": after,
        "screen": _screen(drill, again),
    }


def test_measure_bang_shell_premise(drill: Drill) -> None:  # noqa: F811
    definition = next(item for item in closed_profiles() if item.profile_id == ProfileId("codex"))
    first = _launch(drill, "plain-first", (), _MARKER)
    presets = [
        _launch(
            drill, f"preset-{preset.value}".replace("_", "-"), definition.permission_argv(preset)
        )
        for preset in PermissionPreset
    ]
    read_only = _launch(drill, "read-only", ("--sandbox", "read-only"))
    second = _launch(drill, "plain-second", ())
    after_new = _after_new(drill)
    resume = _resume_bang_only(drill)
    daemons = second["daemons"]
    result = {
        "one_daemon": len(daemons) == 1 and first["daemons"] == daemons,
        "control_marker_seen": second["marker_seen"] is True,
        "first": first,
        "presets": presets,
        "read_only": read_only,
        "second": second,
        "after_new": after_new,
        "resume_bang_only": resume,
    }
    out = os.environ.get("RA_PREMISE_OUT")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert result["one_daemon"], daemons
    assert result["control_marker_seen"], second
