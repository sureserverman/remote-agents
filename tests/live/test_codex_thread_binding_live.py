"""Live premise drill: can a `!` command the service types record a thread binding?

The binding plan has the service type one `!` command into its own pane. The tool shell's session
variable cannot name a session under the shared daemon, and the thread variable is the only fact
each shell holds for itself (DEC-128). So this measures what the design relies on, for the
launches the service really makes: every permission preset's own flags, on one daemon, with two
plain panes around them as the control.

Each preset's `!` shell writes to a folder beside its workspace, the way the activity directory is
outside it. That folder is outside Codex's own sandbox and inside this drill's bwrap, so `outside_*`
says whether Codex confines a `!` shell, and nothing about the real activity directory's path.
`joined_daemon` reads the marker from an agent turn; it is None when the turn wrote nothing. The
drill also records the footer's warning, whether `/new` changes the thread, and whether a thread
holding only a `!` command can be resumed once its pane has ended. The result goes to
`RA_PREMISE_OUT` after every launch, so a later failure keeps it. Only the control is asserted here:
the results are for reading.

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


def _bind_script(outside: Path) -> str:
    """What the `!` shell runs. The turn runs `token.sh`, which stays inside the workspace."""
    return f'{{ printf "%s" "${{CODEX_THREAD_ID-}}" > "{outside}"; }} 2> outside-error.txt\n'


def _pane_state(drill: Drill, name: str) -> str:  # noqa: F811
    """`0` while the pane runs, `1 <status>` once it has ended."""
    return _tmux(
        drill.socket, "list-panes", "-t", f"={name}:", "-F", "#{pane_dead} #{pane_dead_status}"
    ).stdout.strip()


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
    (workspace / "token.sh").write_text(_TOKEN_SCRIPT, encoding="utf-8")
    (workspace / "bind.sh").write_text(_bind_script(outside), encoding="utf-8")
    pane = _open(drill.socket, name, workspace, drill.codex_home, session, (*drill.command, *flags))
    _send(drill.socket, pane, "!sh token.sh seen-bang.txt")
    bang = _read_seen(workspace / "seen-bang.txt", 30.0)
    _settled(drill, pane)
    _send(drill.socket, pane, "!sh bind.sh")
    error = workspace / "outside-error.txt"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not error.is_file():
        time.sleep(0.5)
    time.sleep(0.5)
    _settled(drill, pane)
    created = outside.is_file()
    written = outside.read_text(encoding="utf-8") if created else None
    _send(drill.socket, pane, _TURN)
    turn = _read_seen(workspace / "seen-turn.txt", 60.0 if "read-only" in flags else 180.0)
    state = _pane_state(drill, name)
    screen = _footnote(drill.socket, pane)
    processes = _processes(drill.codex_home)
    return {
        "name": name,
        "flags": list(flags),
        "bang": bang,
        "outside_created": created,
        "bang_wrote": bool(written),
        "bang_error": error.read_text(encoding="utf-8") if error.is_file() else None,
        "bang_thread": written or None,
        "turn": turn,
        "turn_thread": None if turn is None else turn["thread"],
        "daemons": _daemons(processes),
        # The pane that carries the marker sees it whatever the daemon does: no evidence.
        "joined_daemon": None if turn is None or session == _MARKER else turn["session"] == _MARKER,
        "marker_seen": None if turn is None else turn["session"] == _MARKER,
        "pane_state": state,
        "screen": screen,
    }


def _after_new(drill: Drill) -> dict[str, object]:  # noqa: F811
    """What `/new` does to the thread a `!` shell is told."""
    workspace = _workspace(drill, "after-new")
    script = workspace / "token.sh"
    script.write_text(_TOKEN_SCRIPT, encoding="utf-8")
    pane = _open(drill.socket, "after-new", workspace, drill.codex_home, None, drill.command)
    _send(drill.socket, pane, f"!sh {script} {workspace / 'before.txt'}")
    before = _read_seen(workspace / "before.txt", 30.0)
    _settled(drill, pane)
    _send(drill.socket, pane, "/new")
    # A line typed about 3 s after `/new` lost its start and reached the agent as a prompt.
    time.sleep(15.0)
    _settled(drill, pane)
    # Absolute: `/new` has been seen to move the pane to a Codex worktree under its home.
    _send(drill.socket, pane, f"!sh {script} {workspace / 'after.txt'}")
    after = _read_seen(workspace / "after.txt", 60.0)
    return {
        "before": before,
        "after": after,
        # None is unmeasured, never "kept".
        "thread_changed": None
        if before is None or after is None
        else before["thread"] != after["thread"],
        "screen": _screen(drill, pane),
    }


def _resume_bang_only(drill: Drill) -> dict[str, object]:  # noqa: F811
    """Whether a thread that only ever ran a `!` command can be resumed, as a restart would."""
    workspace = _workspace(drill, "bang-only")
    (workspace / "token.sh").write_text(_TOKEN_SCRIPT, encoding="utf-8")
    pane = _open(drill.socket, "bang-only", workspace, drill.codex_home, None, drill.command)
    _send(drill.socket, pane, "!sh token.sh before.txt")
    before = _read_seen(workspace / "before.txt", 30.0)
    _settled(drill, pane)
    _send(drill.socket, pane, "/exit")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not _pane_state(drill, "bang-only").startswith("1"):
        time.sleep(0.5)
    ended = _pane_state(drill, "bang-only")
    rollouts = [
        path.name
        for path in (drill.codex_home / "sessions").glob("*/*/*/rollout-*.jsonl")
        if before is not None and before["thread"] in path.name
    ]
    if before is None:
        return {
            "ended": ended,
            "before": None,
            "rollouts": rollouts,
            "resumed": None,
            "screen": _screen(drill, pane),
        }
    command = (*drill.command, "resume", before["thread"])
    try:
        again = _open(drill.socket, "bang-resume", workspace, drill.codex_home, None, command)
    except (Exception, pytest.fail.Exception) as error:  # the failure is the measurement
        return {
            "before": before,
            "ended": ended,
            "rollouts": rollouts,
            "resumed": False,
            "error": repr(error),
        }
    _send(drill.socket, again, "!sh token.sh after.txt")
    after = _read_seen(workspace / "after.txt", 30.0)
    return {
        "before": before,
        "ended": ended,
        "rollouts": rollouts,
        # Only a pane that ended shows what a restart would find.
        "resumed": ended.startswith("1")
        and after is not None
        and after["thread"] == before["thread"],
        "after": after,
        "screen": _screen(drill, again),
    }


def test_measure_bang_shell_premise(drill: Drill) -> None:  # noqa: F811
    definition = next(item for item in closed_profiles() if item.profile_id == ProfileId("codex"))
    result: dict[str, object] = {
        "one_daemon": None,
        "control_marker_seen": None,
        "presets": [],
    }

    def keep() -> None:
        out = os.environ.get("RA_PREMISE_OUT")
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(result, indent=2), encoding="utf-8")

    def launch(name: str, flags: tuple[str, ...], session: str | None = None) -> dict[str, object]:
        try:
            return _launch(drill, name, flags, session)
        except (Exception, pytest.fail.Exception) as error:
            return {"name": name, "flags": list(flags), "failed": repr(error)}

    first = result["first"] = launch("plain-first", (), _MARKER)
    keep()
    assert len(PermissionPreset) == 4
    for preset in PermissionPreset:
        name = f"preset-{preset.value}".replace("_", "-")
        result["presets"].append(launch(name, definition.permission_argv(preset)))
        keep()
    result["read_only"] = launch("read-only", ("--sandbox", "read-only"))
    keep()
    second = result["second"] = launch("plain-second", ())
    keep()
    result["after_new"] = _after_new(drill)
    keep()
    result["resume_bang_only"] = _resume_bang_only(drill)
    daemons = second.get("daemons")
    result["one_daemon"] = bool(daemons) and len(daemons) == 1 and first.get("daemons") == daemons
    result["control_marker_seen"] = second.get("joined_daemon") is True
    keep()
    assert result["one_daemon"], result
    assert result["control_marker_seen"], second
