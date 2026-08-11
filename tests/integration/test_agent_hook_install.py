"""Installing the agent hooks must survive a real settings file, or refuse to touch it.

Every case here builds its own settings file under ``tmp_path``. Nothing in this module reads,
writes, or names the operator's own ``~/.claude/settings.json``: that file configures the very
agent session this suite is run from, which is why the installer takes the path to operate on
as an argument instead of resolving one for itself.

The fixture below mirrors the shape of a settings file that has been lived in — unrelated
top-level keys, an unrelated ``PostToolUse`` hook, and, critically, a pre-existing
``SessionEnd`` hook belonging to somebody else. ``SessionEnd`` is one of the four events this
installer adds, so a fixture with an empty ``hooks`` block would let a merge that clobbers an
event array pass unnoticed.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from remote_agents.adapters.agents.hook_install import (
    INSTALLED_EVENTS,
    HookInstallError,
    agent_event_command,
    install_agent_hooks,
    remove_agent_hooks,
)
from remote_agents.bootstrap import main

_LIVED_IN_SETTINGS = {
    "agentPushNotifEnabled": True,
    "effortLevel": "high",
    "enabledPlugins": {"planning@opaque-kit": True},
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": 'bash "/home/tester/lint.sh"'}],
            }
        ],
        "SessionEnd": [
            {"hooks": [{"type": "command", "command": 'bash "/home/tester/session-end.sh"'}]}
        ],
    },
    "model": "opus",
    "permissions": {"allow": ["Bash(git status:*)"]},
    "statusLine": {"type": "command", "command": "statusline.sh"},
}


def _settings_file(directory: Path, document: object = _LIVED_IN_SETTINGS, **style) -> Path:
    """Write a settings file in a caller-chosen formatting style and return its path."""
    path = directory / "settings.json"
    text = json.dumps(document, indent=style.get("indent", 2))
    path.write_text(text + "\n" if style.get("trailing_newline", True) else text, encoding="utf-8")
    return path


def _installed_commands(path: Path, event: str) -> list[str]:
    """Return the commands of the groups this installer owns under one event."""
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"][event]
    return [
        entry["command"]
        for group in groups
        for entry in group["hooks"]
        if "remote_agents agent-event" in entry["command"]
    ]


def test_install_adds_the_four_events_and_preserves_everything_unrelated(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)

    install_agent_hooks(path)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert {key: value for key, value in document.items() if key != "hooks"} == {
        key: value for key, value in _LIVED_IN_SETTINGS.items() if key != "hooks"
    }
    assert document["hooks"]["PostToolUse"] == _LIVED_IN_SETTINGS["hooks"]["PostToolUse"]
    assert document["hooks"]["SessionEnd"][0] == _LIVED_IN_SETTINGS["hooks"]["SessionEnd"][0]
    for event in INSTALLED_EVENTS:
        assert len(_installed_commands(path, event)) == 1


def test_install_names_the_running_interpreter_and_the_package_entry_point(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)

    install_agent_hooks(path, executable=Path("/opt/venv/bin/python3"))

    for event in INSTALLED_EVENTS:
        assert _installed_commands(path, event) == [
            "/opt/venv/bin/python3 -m remote_agents agent-event"
        ]
    assert agent_event_command(Path("/opt/venv/bin/python3")).endswith("agent-event")


def test_installing_twice_leaves_exactly_one_entry_for_each_event(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)

    install_agent_hooks(path)
    after_first = path.read_bytes()
    install_agent_hooks(path)

    assert path.read_bytes() == after_first
    for event in INSTALLED_EVENTS:
        assert len(_installed_commands(path, event)) == 1
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]) == 2


@pytest.mark.parametrize("indent", [2, 4, None])
@pytest.mark.parametrize("trailing_newline", [True, False])
def test_remove_restores_the_file_byte_for_byte(
    tmp_path: Path, indent: int | None, trailing_newline: bool
) -> None:
    path = _settings_file(tmp_path, indent=indent, trailing_newline=trailing_newline)
    before = path.read_bytes()

    install_agent_hooks(path)
    assert path.read_bytes() != before

    remove_agent_hooks(path)

    assert path.read_bytes() == before


def test_remove_restores_byte_for_byte_after_a_repeated_install(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)
    before = path.read_bytes()

    install_agent_hooks(path)
    install_agent_hooks(path, executable=Path("/opt/other/bin/python3"))
    assert _installed_commands(path, "Stop") == [
        "/opt/other/bin/python3 -m remote_agents agent-event"
    ]

    remove_agent_hooks(path)

    assert path.read_bytes() == before


def test_remove_leaves_an_unrelated_hook_under_a_shared_event(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)

    install_agent_hooks(path)
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]) == 2

    remove_agent_hooks(path)

    surviving = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]
    assert surviving == _LIVED_IN_SETTINGS["hooks"]["SessionEnd"]


def test_a_settings_file_that_is_not_valid_json_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"hooks": {,,,}\n')

    with pytest.raises(HookInstallError) as refusal:
        install_agent_hooks(path)

    assert "JSON" in str(refusal.value)
    assert path.read_bytes() == b'{"hooks": {,,,}\n'
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["settings.json"]


@pytest.mark.parametrize(
    "document",
    [
        {"hooks": []},
        {"hooks": "none"},
        {"hooks": {"Stop": {"matcher": ""}}},
        ["not", "an", "object"],
    ],
)
def test_a_shape_that_cannot_be_merged_into_is_refused_and_left_untouched(
    tmp_path: Path, document: object
) -> None:
    path = _settings_file(tmp_path, document)
    before = path.read_bytes()

    with pytest.raises(HookInstallError):
        install_agent_hooks(path)

    assert path.read_bytes() == before


def test_a_file_this_installer_could_not_restore_exactly_is_refused(tmp_path: Path) -> None:
    """An empty ``hooks`` block is indistinguishable from an absent one once installed into.

    Removal cannot know whether it should leave ``"hooks": {}`` behind or delete the key, so
    the install-time self-check refuses instead of promising a restore it cannot deliver.
    """
    path = _settings_file(tmp_path, {"model": "opus", "hooks": {}})
    before = path.read_bytes()

    with pytest.raises(HookInstallError) as refusal:
        install_agent_hooks(path)

    # The refusal is only useful if it tells the operator what to do about it: this is their
    # own settings file, and "could not restore exactly" alone leaves them nowhere to go.
    message = str(refusal.value)
    assert str(path) in message
    assert "empty" in message
    assert "Delete" in message
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "command",
    (
        "echo 'note: never run remote_agents agent-event by hand' && /home/tester/mine.sh",
        "grep -r 'remote_agents agent-event' ~/.claude",
        "/usr/bin/python -m remote_agents agent-event --extra",
        "/usr/bin/python -m remote_agents doctor",
    ),
)
def test_a_hook_that_only_mentions_our_command_is_never_treated_as_ours(
    tmp_path: Path, command: str
) -> None:
    """Mentioning the subcommand and running it are different things, and only one is ours.

    Matching the command as text made an operator's own hook ours whenever its text happened
    to contain the marker -- a reminder, a grep in an auditing script -- and `--remove` then
    deleted it. Nothing here writes that hook, so nothing here may remove it.
    """
    document = {
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]},
    }
    path = _settings_file(tmp_path, document)
    before = path.read_bytes()

    removed = remove_agent_hooks(path)

    assert not removed.changed
    assert path.read_bytes() == before

    install_agent_hooks(path)
    survivors = [
        entry["command"]
        for group in json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]
        for entry in group["hooks"]
    ]

    assert command in survivors


def test_a_symlinked_settings_file_is_written_through_rather_than_replaced(
    tmp_path: Path,
) -> None:
    """Someone whose dotfiles are symlinked into place must still have dotfiles afterwards."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = _settings_file(dotfiles)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    before = real.read_bytes()

    install_agent_hooks(link)

    assert link.is_symlink()
    assert real.read_bytes() != before

    remove_agent_hooks(link)

    assert link.is_symlink()
    assert real.read_bytes() == before


def test_a_settings_file_that_cannot_be_written_is_refused_rather_than_raising(
    tmp_path: Path,
) -> None:
    """A disk or permission failure is still a refusal, so the CLI reports rather than crashes."""
    directory = tmp_path / "readonly"
    directory.mkdir()
    path = _settings_file(directory)
    before = path.read_bytes()
    directory.chmod(0o500)
    try:
        with pytest.raises(HookInstallError):
            install_agent_hooks(path)

        assert path.read_bytes() == before
    finally:
        directory.chmod(0o700)


def test_an_empty_list_under_one_of_our_events_is_refused(tmp_path: Path) -> None:
    """The refusal message promises this case, so something has to hold it to that."""
    path = _settings_file(tmp_path, {"model": "opus", "hooks": {"Stop": []}})
    before = path.read_bytes()

    with pytest.raises(HookInstallError):
        install_agent_hooks(path)

    assert path.read_bytes() == before


def test_an_absent_settings_file_is_created_and_remove_is_a_quiet_no_op(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    assert not remove_agent_hooks(path).changed
    assert not path.exists()

    install_agent_hooks(path)
    assert set(json.loads(path.read_text(encoding="utf-8"))["hooks"]) == set(INSTALLED_EVENTS)

    remove_agent_hooks(path)
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_an_absent_settings_directory_is_refused_rather_than_created(tmp_path: Path) -> None:
    with pytest.raises(HookInstallError):
        install_agent_hooks(tmp_path / "absent" / "settings.json")

    assert not (tmp_path / "absent").exists()


def test_the_file_keeps_its_mode_and_no_temporary_file_survives(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)
    path.chmod(0o600)

    assert install_agent_hooks(path).changed

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["settings.json"]


def test_the_subcommand_installs_and_removes_through_the_named_settings_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _settings_file(tmp_path)
    before = path.read_bytes()

    assert main(["install-agent-hooks", "--settings", str(path)]) == 0
    assert len(_installed_commands(path, "Stop")) == 1

    assert main(["install-agent-hooks", "--settings", str(path), "--remove"]) == 0
    assert path.read_bytes() == before
    assert capsys.readouterr().out.strip() != ""


def test_the_subcommand_reports_a_refusal_on_standard_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"not json at all")

    assert main(["install-agent-hooks", "--settings", str(path)]) == 1

    assert path.read_bytes() == b"not json at all"
    assert capsys.readouterr().err.strip() != ""
