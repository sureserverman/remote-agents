"""The Claude Remote Control default, against real settings files on a temporary home.

Written from the requirement. The requirement has two halves and they pull in opposite
directions, which is most of what this file is about:

- **Reading is total.** A settings file the owner hand-edited into nonsense may not be a reason
  a Settings row will not draw, so every way a read can fail answers `PROVIDER_DEFAULT`.
- **Writing is careful.** The file is `~/.claude/settings.json`: it holds this project's own
  activity hooks, the owner's permissions, their model choice. A write that reformatted it, or
  that was computed from a stale read, would lose those silently. So a write keeps every other
  byte, and refuses rather than guessing.

No `claude` runs and no real home is touched: every test builds a settings file under `tmp_path`.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from remote_agents.adapters.agents.claude.remote_control_default import (
    ClaudeRemoteControlDefault,
)
from remote_agents.domain.remote_control import RemoteControlDefault

ON = RemoteControlDefault.ON
OFF = RemoteControlDefault.OFF
PROVIDER_DEFAULT = RemoteControlDefault.PROVIDER_DEFAULT

#: What the owner's real file looks like in the shape that matters: our hooks, their settings.
#: Taken from the structure of this host's own `~/.claude/settings.json` rather than invented,
#: because "keeps every other key" is only a meaningful claim about a file that has other keys.
A_REAL_LOOKING_FILE = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status:*)"], "deny": []},
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/home/owner/.local/bin/python3 -m remote_agents agent-event",
                    }
                ]
            }
        ]
    },
    "statusLine": {"type": "command", "command": "~/.claude/statusline.sh"},
}


def _settings(tmp_path: Path, document: object, *, indent: int | None = 2) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=indent)
    path.write_text(f"{text}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _port(path: Path) -> ClaudeRemoteControlDefault:
    return ClaudeRemoteControlDefault(path)


# --- Reading -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, ON),
        (False, OFF),
    ],
)
async def test_a_stored_boolean_reads_as_that_state(
    tmp_path: Path, stored: bool, expected: RemoteControlDefault
) -> None:
    path = _settings(tmp_path, A_REAL_LOOKING_FILE | {"remoteControlAtStartup": stored})
    assert await _port(path).read() is expected


async def test_an_absent_key_reads_as_the_provider_default(tmp_path: Path) -> None:
    """The measured case: absent means Claude's account default decides, and it decided *on*.

    So this must not read `OFF` (acceptance section 8).
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    assert await _port(path).read() is PROVIDER_DEFAULT


@pytest.mark.parametrize(
    "stored",
    [
        1,
        0,
        "true",
        "on",
        None,
        [],
        {},
        "",
    ],
    ids=["one", "zero", "string-true", "string-on", "null", "array", "object", "empty-string"],
)
async def test_a_wrong_typed_value_reads_as_the_provider_default(
    tmp_path: Path, stored: object
) -> None:
    """`1` and `0` are the two that matter, and they are why the check is `is True`.

    In Python `1 == True`, so a reader written as `stored == True` would report `ON` for a file
    holding the number one -- a value `claude` itself would not accept as the boolean its schema
    declares. Answering the provider default instead says "this file does not tell me", which is
    the truth.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE | {"remoteControlAtStartup": stored})
    assert await _port(path).read() is PROVIDER_DEFAULT


async def test_an_absent_file_reads_as_the_provider_default(tmp_path: Path) -> None:
    assert await _port(tmp_path / ".claude" / "settings.json").read() is PROVIDER_DEFAULT


async def test_an_empty_file_reads_as_the_provider_default(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert await _port(path).read() is PROVIDER_DEFAULT


async def test_a_malformed_file_reads_as_the_provider_default(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"remoteControlAtStartup": tru', encoding="utf-8")
    assert await _port(path).read() is PROVIDER_DEFAULT


async def test_a_non_utf8_file_reads_as_the_provider_default(tmp_path: Path) -> None:
    """A `UnicodeDecodeError` is a `ValueError`, not an `OSError`.

    The class this repo has swept four times before; the fifth instance is recorded in
    `adapters/tui/preferences.py`. A row that crashed the surface over one byte would be the
    sixth.
    """
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{\x00")
    assert await _port(path).read() is PROVIDER_DEFAULT


async def test_a_file_holding_an_array_reads_as_the_provider_default(tmp_path: Path) -> None:
    path = _settings(tmp_path, ["remoteControlAtStartup"])
    assert await _port(path).read() is PROVIDER_DEFAULT


async def test_an_unreadable_file_reads_as_the_provider_default(tmp_path: Path) -> None:
    path = _settings(tmp_path, A_REAL_LOOKING_FILE | {"remoteControlAtStartup": True})
    os.chmod(path, 0o000)
    try:
        assert await _port(path).read() is PROVIDER_DEFAULT
    finally:
        os.chmod(path, 0o600)


# --- Writing -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (ON, True),
        (OFF, False),
    ],
)
async def test_a_write_stores_the_boolean_claude_reads(
    tmp_path: Path, value: RemoteControlDefault, stored: bool
) -> None:
    """The spelling is `claude`'s, not ours: its schema declares a boolean, so a boolean lands.

    Asserted against the file's own JSON rather than through `read()`, because a round trip
    through our own reader would pass for any encoding the two agreed on -- including one
    `claude` does not accept.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    await _port(path).write(value)
    assert json.loads(path.read_text())["remoteControlAtStartup"] is stored


async def test_writing_the_provider_default_removes_the_key_entirely(tmp_path: Path) -> None:
    """Not `null`, not a third value: gone, so `claude` resolves its own default afresh.

    A file holding `"remoteControlAtStartup": null` would be this project's idea of "unset"
    written into somebody else's schema.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE | {"remoteControlAtStartup": True})
    await _port(path).write(PROVIDER_DEFAULT)
    assert "remoteControlAtStartup" not in json.loads(path.read_text())


async def test_a_write_keeps_every_other_key_including_our_own_hooks(tmp_path: Path) -> None:
    """The claim the whole careful-write machinery exists for.

    `hooks` is singled out in the name because losing it is the worst outcome available here:
    this project's own activity spool lives there, so a write that dropped it would break the
    feature that reports what the agents are doing -- silently, and from a Settings screen the
    owner pressed expecting nothing else to change.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    await _port(path).write(ON)
    document = json.loads(path.read_text())
    assert document["hooks"] == A_REAL_LOOKING_FILE["hooks"]
    assert document["permissions"] == A_REAL_LOOKING_FILE["permissions"]
    assert document["model"] == A_REAL_LOOKING_FILE["model"]
    assert document["statusLine"] == A_REAL_LOOKING_FILE["statusLine"]
    assert set(document) == set(A_REAL_LOOKING_FILE) | {"remoteControlAtStartup"}


@pytest.mark.parametrize("indent", [2, 4, None], ids=["two-space", "four-space", "compact"])
async def test_a_write_keeps_the_files_own_formatting(tmp_path: Path, indent: int | None) -> None:
    """Byte-compared, by removing the key again and checking the file came back identical.

    The strongest available statement of "did not reformat": if the style round trip were
    approximate, a set-then-clear would leave a differently-indented file holding the same
    document, and nothing about the document would show it.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE, indent=indent)
    before = path.read_bytes()
    port = _port(path)
    await port.write(OFF)
    assert path.read_bytes() != before
    await port.write(PROVIDER_DEFAULT)
    assert path.read_bytes() == before


async def test_a_write_keeps_the_files_mode(tmp_path: Path) -> None:
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    os.chmod(path, 0o600)
    await _port(path).write(ON)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_a_file_created_from_nothing_is_owner_only(tmp_path: Path) -> None:
    """A fresh machine has no settings file, and the one this creates must not be readable.

    `0600` because the file it is standing in for holds the owner's permissions grants; a
    world-readable one would be a downgrade this project introduced.
    """
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    await _port(path).write(ON)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"remoteControlAtStartup": True}


async def test_a_write_survives_a_round_trip_through_the_reader(tmp_path: Path) -> None:
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    port = _port(path)
    for value in (ON, OFF, PROVIDER_DEFAULT, ON):
        await port.write(value)
        assert await port.read() is value


async def test_a_write_to_an_unreproducible_file_is_one_warning_and_no_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file with a comment, or odd whitespace, cannot be rendered back byte-for-byte.

    The machinery refuses rather than reformatting, and the port turns that into a warning: the
    owner's press is forgotten, which the next `read` reports honestly, and the file is
    untouched. Raising instead would take down the screen over somebody's formatting.
    """
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    original = b'{\n      "model": "opus"   \n}\n'
    path.write_bytes(original)
    with caplog.at_level("WARNING"):
        await _port(path).write(ON)
    assert path.read_bytes() == original
    assert len(caplog.records) == 1


async def test_a_write_into_a_missing_directory_is_one_warning_and_no_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No `~/.claude` at all means `claude` has never run here; creating one is not our call."""
    with caplog.at_level("WARNING"):
        await _port(tmp_path / "absent" / "settings.json").write(ON)
    assert len(caplog.records) == 1


async def test_a_file_changed_since_it_was_read_is_refused_rather_than_clobbered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude` writes this file itself, from a session plausibly running while this is pressed.

    A whole-file replace computed from a stale read would discard whatever landed in between --
    an "always allow" grant, a model change. The interleaving is forced here by writing to the
    file between the read and the render, which is the window the refusal covers.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    from remote_agents.adapters.agents import hook_settings

    real_render = hook_settings._SettingsStyle.render

    def render_after_somebody_else_wrote(self: object, document: object) -> bytes:
        path.write_text(json.dumps(A_REAL_LOOKING_FILE | {"model": "sonnet"}, indent=2) + "\n")
        return real_render(self, document)

    monkeypatch.setattr(hook_settings._SettingsStyle, "render", render_after_somebody_else_wrote)
    with caplog.at_level("WARNING"):
        await _port(path).write(ON)
    document = json.loads(path.read_text())
    assert document["model"] == "sonnet", "the concurrent write was discarded"
    assert "remoteControlAtStartup" not in document
    assert len(caplog.records) == 1


async def test_writing_the_provider_default_to_a_file_without_the_key_writes_nothing(
    tmp_path: Path,
) -> None:
    """Removing what is not there is not a change, so the file is not rewritten at all."""
    path = _settings(tmp_path, A_REAL_LOOKING_FILE)
    before = path.stat().st_mtime_ns, path.read_bytes()
    await _port(path).write(PROVIDER_DEFAULT)
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


# --- Where the file is ---------------------------------------------------------------------


def test_the_factory_points_the_port_at_the_file_the_hook_installer_edits(tmp_path: Path) -> None:
    """One spelling of "Claude's settings file" in the project, not two.

    The resolution is the registry's, not this class's -- a vertical asking the registry where its
    own config lives is a cycle, since the registry is the only module allowed to import the
    vertical. So what is pinned here is that the factory and the hook installer agree: if they
    drifted, the Settings row would read and write a file `claude` does not load, with no symptom
    but the feature quietly not working.
    """
    from remote_agents.adapters.agents.registry import (
        claude_remote_control_default,
        default_settings_path,
    )

    port = claude_remote_control_default(tmp_path)
    assert port.settings_path == default_settings_path(tmp_path, provider="claude")
    assert port.settings_path == tmp_path / ".claude" / "settings.json"


def test_the_key_is_spelled_the_way_claude_spells_it() -> None:
    """A typo here is a row that reads and writes a key nothing consumes, silently.

    Pinned as a literal because the name is `claude`'s, taken from its own settings schema
    (`--help`'s `/config` row *Enable Remote Control for all sessions*, and the binary's
    `remoteControlAtStartup: "Start Remote Control bridge automatically each session"`).
    """
    from remote_agents.adapters.agents.claude.remote_control_default import (
        REMOTE_CONTROL_AT_STARTUP_KEY,
    )

    assert REMOTE_CONTROL_AT_STARTUP_KEY == "remoteControlAtStartup"


# --- The wedge the gate's adversarial review found ------------------------------------------


@pytest.mark.parametrize("stored", [1, 1.0, 0, 0.0], ids=["one", "one-float", "zero", "zero-float"])
async def test_a_numeric_value_does_not_wedge_the_control(tmp_path: Path, stored: object) -> None:
    """A file holding `1` must not make the row impossible to move.

    **The defect this pins was real, silent and permanent.** `_rewrite_settings_key`'s no-op guard
    compared *documents*, and `==` on dicts compares values with `==`, under which `True == 1`. So
    with `1` in the file: the reader answered `PROVIDER_DEFAULT` (it compares by identity, which is
    right), the cycle advanced to `ON`, the writer computed `{... key: True} == {... key: 1}` →
    `True` and returned having written nothing, with no exception and no log line. The next read
    still said `PROVIDER_DEFAULT`. The row could never move, and the terminal's status line
    cheerfully reported the unchanged value after every press.

    `0` escaped it by luck -- `True != 0` -- which is exactly why the parametrisation covers both
    and both float forms: the bug was in one pair of values, so a single-value test would have
    passed on three of the four.
    """
    path = _settings(tmp_path, A_REAL_LOOKING_FILE | {"remoteControlAtStartup": stored})
    port = _port(path)
    assert await port.read() is PROVIDER_DEFAULT, "a non-boolean does not answer the question"

    await port.write(ON)

    assert json.loads(path.read_text())["remoteControlAtStartup"] is True
    assert await port.read() is ON


async def test_every_state_is_reachable_from_a_numeric_value(tmp_path: Path) -> None:
    """The whole cycle, walked from the wedged starting point rather than from a clean file."""
    path = _settings(tmp_path, A_REAL_LOOKING_FILE | {"remoteControlAtStartup": 1})
    port = _port(path)
    seen = set()
    for value in (ON, OFF, PROVIDER_DEFAULT):
        await port.write(value)
        seen.add(await port.read())
    assert seen == {ON, OFF, PROVIDER_DEFAULT}


async def test_clearing_a_key_in_a_file_that_does_not_exist_creates_nothing(tmp_path: Path) -> None:
    """`clear_settings_key` promised this and did the opposite: it wrote `{}`.

    Removing a key from a file that is not there is not a change, so recording it by *creating*
    the file is the one outcome that cannot be right -- and for this port it would mean a press
    on a host where `claude` has never run leaving a settings file behind.
    """
    from remote_agents.adapters.agents.hook_settings import clear_settings_key

    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)

    clear_settings_key(path, "remoteControlAtStartup")

    assert not path.exists(), f"the file was created holding {path.read_bytes()!r}"
