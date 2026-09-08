"""The generated OpenCode plugin, driven by a real JavaScript runtime against real captures.

The plugin is the one artifact in this project that runs inside somebody else's process in
somebody else's language, so asserting its *source text* would prove nothing: a regex over
generated JavaScript cannot tell a handler that drops `metadata` from one that reads it and
then discards it later. `node` is therefore driven for real, with the plugin's spawned command
pointed at a capture script, and what is asserted is the document the plugin actually delivers
to `spool_agent_event`'s stdin.

The payloads come from `fixtures/opencode/*.json`, which carry their own capture provenance
(`docs/acceptance-2026-09-06-opencode-activity.md`) and whose identifiers and commands are
synthetic per GDEC-SEC-001 — deliberately recognisable, so the leak assertions below can name
the exact string that must not appear.

**A missing `node` skips rather than fails**, and the skip carries its reason (DEC-059: what is
not run is a named set). CI asserts `node --version` in its dependency step, so the skip is a
developer-machine allowance and never a hole in the badge.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from remote_agents.adapters.agents.opencode.plugin import PLUGIN_RELATIVE_PATH, plugin_source

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "opencode"

#: The literal command the `permission.asked` capture carries in three separate places
#: (`patterns`, `metadata.command`, and — as a glob — `always`). Nothing this plugin delivers
#: may contain any part of it.
_SYNTHETIC_COMMAND = "rm -rf /home/owner/secret-project"

#: The plugin factory's argument, shaped as OpenCode's own documented `PluginInput` rather than
#: as an empty object. The plugin ignores it today; the harness carries the real keys so that an
#: edit which starts destructuring one is checked against the shape OpenCode actually passes,
#: instead of against a stub that would accept anything. Raised as a Tier-1 suggestion.
_PLUGIN_INPUT = """{{
  client: {{}},
  app: {{}},
  $: () => {{}},
  directory: process.cwd(),
  worktree: process.cwd(),
}}"""

_HARNESS = """
import * as plugin from "./{module}";

const event = JSON.parse(process.argv[2]);
for (const exported of Object.values(plugin)) {{
  if (typeof exported !== "function") continue;
  const hooks = await exported({input});
  if (hooks && typeof hooks.event === "function") await hooks.event({{ event }});
}}
"""

_CAPTURE = """
import sys
from pathlib import Path

Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())
"""


def _node() -> str:
    found = shutil.which("node")
    if found is None:
        pytest.skip("no `node` on this machine, so the generated plugin cannot be executed")
    return found


def _event(name: str) -> dict:
    """One captured payload, with the fixture's own provenance keys taken back off."""
    document = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return {key: value for key, value in document.items() if not key.startswith("_")}


def _run(
    tmp_path: Path, event: dict, command: list[str], *, session_id: str | None, timeout: float
) -> float:
    """Render the plugin around `command`, drive one event through node, return the elapsed."""
    node = _node()
    plugin = tmp_path / PLUGIN_RELATIVE_PATH.name
    plugin.write_text(plugin_source(command), encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        _HARNESS.format(module=plugin.name, input=_PLUGIN_INPUT.format()), encoding="utf-8"
    )

    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if session_id is not None:
        environment["REMOTE_AGENTS_SESSION_ID"] = session_id
    started = time.monotonic()
    finished = subprocess.run(
        [node, str(harness), json.dumps(event)],
        capture_output=True,
        env=environment,
        timeout=timeout,
    )
    assert finished.returncode == 0, finished.stderr.decode()
    return time.monotonic() - started


def _drive(
    tmp_path: Path, event: dict, *, session_id: str | None = "sess-opencode"
) -> bytes | None:
    """Run the generated plugin under node against one event; return what it delivered."""
    delivered = tmp_path / "delivered.json"
    capture = tmp_path / "capture.py"
    capture.write_text(_CAPTURE, encoding="utf-8")

    _run(
        tmp_path,
        event,
        ["python3", str(capture), str(delivered)],
        session_id=session_id,
        timeout=60,
    )
    return delivered.read_bytes() if delivered.exists() else None


def test_session_idle_delivers_its_occurrence_and_nothing_from_the_payload(tmp_path: Path) -> None:
    """The event's occurrence is the whole signal — not even OpenCode's own session id."""
    delivered = _drive(tmp_path, _event("session_idle"))

    assert delivered is not None
    assert json.loads(delivered) == {"hook_event_name": "session.idle"}


def test_permission_asked_delivers_the_tool_class_and_never_the_command(tmp_path: Path) -> None:
    """`properties.permission` at most — `patterns`, `metadata`, `always` and `tool` stay put."""
    delivered = _drive(tmp_path, _event("permission_asked"))

    assert delivered is not None
    assert json.loads(delivered) == {
        "hook_event_name": "permission.asked",
        "permission": "bash",
    }
    text = delivered.decode()
    assert _SYNTHETIC_COMMAND not in text
    for forbidden in ("patterns", "metadata", "always", "tool", "sessionID", "per_synthetic"):
        assert forbidden not in text


def test_an_event_carrying_no_session_id_delivers_nothing(tmp_path: Path) -> None:
    """Identity comes from the environment variable, and without it there is nothing to key."""
    assert _drive(tmp_path, _event("session_idle"), session_id=None) is None


def test_an_empty_session_id_delivers_nothing_either(tmp_path: Path) -> None:
    """An exported-but-empty variable is the same absence, and must not spawn a command."""
    assert _drive(tmp_path, _event("session_idle"), session_id="") is None


@pytest.mark.parametrize(
    "unlicensed",
    [
        {"type": "permission.replied", "properties": {"reply": "reject"}},
        {"type": "plugin.added", "properties": {"name": "somebody-elses"}},
        {"type": "message.part.updated", "properties": {"text": "the agent's own words"}},
        {"type": "session.updated", "properties": {"sessionID": "ses_x"}},
    ],
    ids=["permission.replied", "plugin.added", "message.part.updated", "session.updated"],
)
def test_every_other_event_type_is_dropped_by_name(tmp_path: Path, unlicensed: dict) -> None:
    """The `event` stream carried 14 types across two short runs; 12 of them are not ours."""
    assert _drive(tmp_path, unlicensed) is None


def test_a_permission_ask_without_a_string_class_still_reports_the_wait(tmp_path: Path) -> None:
    """An unmeasured shape costs the ask class, never the notification that one is waiting."""
    event = _event("permission_asked")
    event["properties"]["permission"] = {"not": "a string"}

    delivered = _drive(tmp_path, event)

    assert delivered is not None
    assert json.loads(delivered) == {"hook_event_name": "permission.asked"}


def test_a_spool_command_that_never_finishes_is_killed_and_the_session_moves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded wait must end the child, not merely stop waiting for it.

    A spawned process keeps the host's event loop alive by itself, so a handler that resolved
    its promise and walked away would hand OpenCode a process it cannot exit -- the hang the
    bound exists to prevent, arriving through the bound. What proves the kill happened is that
    `node` itself returns: the command below would otherwise sit there for a full minute, and
    the `timeout` on this run is a fifth of that.

    A Tier-1 review found the leak. It proposed `child.unref()` alongside the kill; that half is
    deliberately not taken, because unref'ing the child as well as the timer lets node exit with
    the delivery still pending, which loses the `session.idle` record the wait was added for.
    """
    monkeypatch.setattr(
        "remote_agents.adapters.agents.opencode.plugin._WAIT_MILLISECONDS", 300
    )

    elapsed = _run(
        tmp_path,
        _event("session_idle"),
        ["python3", "-c", "import time; time.sleep(60)"],
        session_id="sess-opencode",
        timeout=12,
    )

    assert elapsed < 10, "node did not exit, so the hung spool command was left holding it open"


def test_a_child_that_ignores_sigterm_does_not_hold_the_host_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kill()` sends SIGTERM, and SIGTERM is a request. A wedged child must not outrank it.

    The sibling case above proves the kill works on a child that respects it, and an adversarial
    review measured what that test could not see: a child ignoring SIGTERM left node alive past
    20 seconds, because a referenced child holds the event loop open whether it is dying or not.
    The child is now released as well as signalled, which is safe only at this point -- the wait
    has expired and the record is already given up on.
    """
    monkeypatch.setattr("remote_agents.adapters.agents.opencode.plugin._WAIT_MILLISECONDS", 300)

    elapsed = _run(
        tmp_path,
        _event("session_idle"),
        [
            "python3",
            "-c",
            "import signal,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n",
        ],
        session_id="sess-opencode",
        timeout=12,
    )

    assert elapsed < 10, "node was held open by a child that would not take SIGTERM"


def test_the_handler_called_with_no_argument_does_not_throw(tmp_path: Path) -> None:
    """"Every path is inside a try" has to include reading the argument.

    Destructuring in the parameter list runs before the try is entered, so a host calling this
    against its own documented `({ event })` signature would reject with a TypeError nothing
    here could catch -- which is the "nothing can fail the session it runs in" claim failing on
    its own doorstep. Found by an adversarial review.
    """
    node = _node()
    plugin = tmp_path / PLUGIN_RELATIVE_PATH.name
    plugin.write_text(plugin_source(["/nonexistent/interpreter"]), encoding="utf-8")
    harness = tmp_path / "bare.mjs"
    harness.write_text(
        f'import * as plugin from "./{plugin.name}";\n'
        "for (const exported of Object.values(plugin)) {\n"
        '  if (typeof exported !== "function") continue;\n'
        "  const hooks = await exported({});\n"
        '  if (hooks && typeof hooks.event === "function") await hooks.event();\n'
        "}\n",
        encoding="utf-8",
    )

    finished = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "REMOTE_AGENTS_SESSION_ID": "s"},
        timeout=60,
    )

    assert finished.returncode == 0, finished.stderr.decode()
    assert b"TypeError" not in finished.stderr
