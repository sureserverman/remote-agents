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
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.agents import registry as hook_install
from remote_agents.adapters.agents.claude.hooks import INSTALLED_EVENTS, RETIRED_EVENTS
from remote_agents.adapters.agents.hook_settings import (
    HookInstallError,
    _without_our_groups,
)
from remote_agents.adapters.agents.registry import (
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
    # `SessionEnd` is retired (DEC-051), so the operator's own hook is the only thing left
    # under it — this installer neither adds to it nor takes the stranger away.
    assert (
        json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]
        == (_LIVED_IN_SETTINGS["hooks"]["SessionEnd"])
    )


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


def test_upgrading_removes_a_group_left_by_a_version_that_still_installed_it() -> None:
    """The retirement cleans up after itself, which the naive one-line change would not have.

    `_without_our_groups` opens by skipping any event it does not own, so an event simply
    deleted from `INSTALLED_EVENTS` stops being inspected — and our group under it survives
    both the install path and the uninstall path, which share the predicate. The hook would
    fire on every host for ever with none of this project's tooling able to remove it, and it
    could not be worked around by uninstalling first, since that means running the *old*
    uninstall before taking the upgrade.

    `RETIRED_EVENTS` is what closes that, and this is the case it exists for: a settings file
    written by a version that still installed `SessionEnd`, read by one that does not.
    """
    ours = "/venv/bin/python -m remote_agents agent-event"
    lived_in = {
        "hooks": {
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": ours}]},
                {"hooks": [{"type": "command", "command": 'bash "/home/tester/session-end.sh"'}]},
            ]
        }
    }

    from remote_agents.adapters.agents.registry import _CLAUDE

    kept = _without_our_groups(lived_in, _CLAUDE)

    assert kept["hooks"]["SessionEnd"] == [
        {"hooks": [{"type": "command", "command": 'bash "/home/tester/session-end.sh"'}]}
    ], "the retired group was stranded, or the operator's own hook was taken with it"


def test_every_retired_event_has_left_the_installed_set() -> None:
    """The two tuples are disjoint, so an event cannot be both owned and retired.

    An event in both would be installed and then swept in the same run, which reads as an
    installer that cannot make up its mind and would leave the file churning between two
    states on alternate runs.
    """
    assert not set(INSTALLED_EVENTS) & set(RETIRED_EVENTS)


def test_remove_leaves_an_unrelated_hook_under_a_shared_event(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)

    install_agent_hooks(path)
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]) == 1

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
        "/usr/bin/python -m remote_agents agent-event --activity-dir",
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


def test_a_group_the_operator_narrowed_with_a_matcher_is_not_ours_to_delete(
    tmp_path: Path,
) -> None:
    """This installer writes matcherless groups, so a group with a matcher is not one of ours.

    `_is_our_group` looked only at the commands and ignored every other key, so a group an
    operator had narrowed with a `matcher` and given a `timeout` was claimed, and `--remove`
    deleted their customization with no record of it. Install did it more quietly still:
    the group was stripped and re-added bare, so the matcher and timeout simply vanished.
    That is the outcome this module says it exists to prevent -- failing to remove a hook is
    recoverable, deleting somebody else's is not.
    """
    command = agent_event_command(Path(sys.executable))
    document = {
        "model": "opus",
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "timeout": 5,
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        },
    }
    path = _settings_file(tmp_path, document)
    before = path.read_bytes()

    assert not remove_agent_hooks(path).changed
    assert path.read_bytes() == before

    install_agent_hooks(path)
    stop = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]

    # Ours is added beside theirs; theirs keeps both keys it was given.
    theirs = {"matcher": "*", "timeout": 5, "hooks": [{"type": "command", "command": command}]}
    assert theirs in stop

    remove_agent_hooks(path)
    assert path.read_bytes() == before


def test_an_unrecognised_variant_of_our_command_is_reported_rather_than_silently_doubled(
    tmp_path: Path,
) -> None:
    """Refusing to touch it is right; saying nothing about it is not.

    A command running our subcommand with a flag this installer does not know is deliberately
    left alone -- it is a wrapper, a hand-edit, or a future version, and removing it would be
    guessing. But install then appends its own group beside it, so the agent runs the hook
    twice per event, and `--remove` later takes only ours and leaves theirs spooling forever.
    The conservative choice is kept; what changes is that the operator is told they have one.
    """
    command = f"{sys.executable} -m remote_agents agent-event --verbose"
    document = {
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]},
    }
    path = _settings_file(tmp_path, document)

    outcome = install_agent_hooks(path)

    assert outcome.changed
    assert "Stop" in outcome.summary
    assert "twice" in outcome.summary

    # Re-running is what an operator does while working out why events arrive twice, so the
    # no-op path has to say it too rather than answering "already current" and nothing else.
    repeated = install_agent_hooks(path)
    assert not repeated.changed
    assert "twice" in repeated.summary
    # Still left strictly alone, which is the half that was already right.
    survivors = [
        entry["command"]
        for group in json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]
        for entry in group["hooks"]
    ]
    assert command in survivors


def test_a_write_landing_while_this_one_computes_is_refused_rather_than_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is read, computed against, then replaced whole -- so a writer in that window loses.

    Claude Code writes this same file itself: a model change, an "always allow" grant. This
    command is plausibly run from inside a session that does exactly that, and `os.replace` of
    content built from the *old* bytes discards the grant entirely. Everything else in this
    module exists to leave the file as it was found apart from four groups, and this was the
    one remaining way that promise broke silently.

    The concurrent write is injected at a real point in the sequence -- after the read, before
    the replace -- rather than simulated after the fact.
    """
    path = _settings_file(tmp_path)
    real = hook_install._refuse_when_removal_would_not_restore

    def land_a_concurrent_write(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)  # type: ignore[arg-type]
        document = dict(_LIVED_IN_SETTINGS)
        document["permissions"] = {"allow": ["Bash(git status:*)", "Bash(git diff:*)"]}
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        hook_install, "_refuse_when_removal_would_not_restore", land_a_concurrent_write
    )

    with pytest.raises(HookInstallError) as refusal:
        install_agent_hooks(path)

    assert "changed" in str(refusal.value)
    # The grant that landed in the window is still there, which is the whole point.
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["permissions"]["allow"] == ["Bash(git status:*)", "Bash(git diff:*)"]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["settings.json"]


def test_a_spool_this_installer_redirected_is_still_its_own_to_remove(tmp_path: Path) -> None:
    """The one option this installer adds must not make its own entry unrecognisable."""
    path = _settings_file(tmp_path)
    before = path.read_bytes()

    install_agent_hooks(path, activity_directory=tmp_path / "somewhere else")
    installed = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]

    assert "--activity-dir" in installed[0]["hooks"][0]["command"]

    remove_agent_hooks(path)

    assert path.read_bytes() == before


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


def test_a_null_under_one_of_our_events_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    """A JSON null defeats a `.get` default, which is a traceback rather than a refusal.

    `_refuse_unmergeable_hooks` lets a null through on purpose -- it reads as "absent", the
    same as a missing key -- so the merge is what has to survive it. It did not: an explicit
    null came back from `.get(event, ())` and unpacking it raised `TypeError` out through the
    CLI, which prints a traceback and tells the operator nothing they can act on. This module
    promises the opposite for every other malformed shape, and the file is what it protects.
    """
    path = _settings_file(tmp_path, {"model": "opus", "hooks": {"Stop": None}})
    before = path.read_bytes()

    with pytest.raises(HookInstallError) as refusal:
        install_agent_hooks(path)

    # Refusing is only half of it: the operator has to be told which shape is theirs, and a
    # message naming only the empty block and the empty list does not describe a null.
    assert "null" in str(refusal.value)
    assert path.read_bytes() == before
    # A refusal that left a temporary behind would be its own defect.
    assert list(tmp_path.iterdir()) == [path]


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


def test_the_installed_hook_command_does_not_load_the_composition_root(tmp_path: Path) -> None:
    """The command installed here fires in every Claude session on the machine, managed or not.

    `bootstrap` is the composition root: importing it pulls in the Telegram service, httpx,
    sqlite3, rich and asyncio. Measured before this was split, `-m remote_agents agent-event`
    loaded 678 modules and took ~0.25s -- on `Stop`, `StopFailure`, `Notification` and
    `SessionEnd`, for every agent session the operator runs, including the ones this service
    did not start and will do nothing about. The environment gate that makes those a no-op was
    only consulted after all of it.

    Asserting on the module *set* rather than on a wall-clock number, because the cost is the
    import graph and a timing assertion would be flaky on a loaded machine.
    """
    probe = (
        "import sys, runpy\n"
        f"sys.argv = ['remote_agents', 'agent-event', '--activity-dir', {str(tmp_path)!r}]\n"
        "try:\n"
        "    runpy.run_module('remote_agents', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = 'remote_agents.bootstrap' in sys.modules\n"
        "print('loaded' if loaded else 'lean', len(sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        input=json.dumps({"hook_event_name": "Stop"}).encode("utf-8"),
        capture_output=True,
        check=True,
    )

    verdict, modules = completed.stdout.decode("utf-8").split()
    assert verdict == "lean"
    assert int(modules) < 300


def test_a_spool_under_an_ancestor_others_can_write_is_refused_at_install_time(
    tmp_path: Path,
) -> None:
    """`--activity-dir` made the spool's location operator-supplied, so someone has to check it.

    `open_private_directory` refuses to write *through* a planted link and deliberately leaves a
    pre-existing ancestor's mode alone -- those are shared XDG directories, not this project's
    to tighten. Together that leaves a precondition its own docstring names and nothing
    enforces: under a group- or world-writable, non-sticky ancestor, another user can unlink
    the leaf and leave a link in its place, which is the race the walk refuses reintroduced one
    level up.

    Checked here rather than in the hook, because this is the moment the path is *chosen* and
    the only one that can refuse out loud. The hook fires thousands of times and must stay
    silent and fast; an installer runs once and has an operator reading its output.
    """
    loose = tmp_path / "loose"
    # `mkdir()` then `chmod()`, never `mkdir(mode=0o777)`. The mode argument is filtered
    # through the process umask and `chmod` is not, so the one-liner establishes whatever the
    # host's umask permits rather than the precondition this test needs. Measured: under the
    # 0o002 umask of the workstation this suite grew up on, `mkdir(mode=0o777)` yields 0o775 --
    # group-writable, so the refusal fires and the test passes. Under CI's 0o022 it yields
    # 0o755, nothing is group- or world-writable, and there is nothing left to refuse. The
    # test then failed on BOTH runners, which is what makes it a Linux-workstation assumption
    # rather than a macOS one.
    loose.mkdir()
    loose.chmod(0o777)
    path = _settings_file(tmp_path)
    before = path.read_bytes()

    with pytest.raises(HookInstallError) as refusal:
        install_agent_hooks(path, activity_directory=loose / "activity")

    message = str(refusal.value)
    assert str(loose) in message
    assert path.read_bytes() == before


def test_a_spool_under_a_sticky_shared_ancestor_is_accepted(tmp_path: Path) -> None:
    """The sticky bit is what makes a shared directory safe, and /tmp is the ordinary case."""
    shared = tmp_path / "sticky"
    # Same umask defect as its sibling above, and it was hiding rather than failing. Under a
    # 0o022 umask this yielded 0o1755: still sticky, so the install is still accepted and the
    # test still passes -- but on a directory that is not shared-writable at all, which is the
    # condition the sticky bit is being tested *against*. A green assertion about the wrong
    # arrangement is the harder half of this class to notice.
    shared.mkdir()
    shared.chmod(0o1777)
    path = _settings_file(tmp_path)

    assert install_agent_hooks(path, activity_directory=shared / "activity").changed


def test_a_relative_spool_is_refused_rather_than_checked_against_the_wrong_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative --activity-dir makes the ancestor check answer about the installer's cwd.

    The path is embedded in the hook command verbatim, and the hook runs in each Claude
    session's own working directory -- so a relative one names a different directory in every
    project, none of them the one that was checked, and none of them the one the service
    drains. The guard passed while being asked about the wrong thing entirely, which is worse
    than not having been asked: it reports safe.
    """
    monkeypatch.chdir(tmp_path)
    path = _settings_file(tmp_path)
    before = path.read_bytes()

    with pytest.raises(HookInstallError) as refusal:
        install_agent_hooks(path, activity_directory=Path("spool"))

    assert "absolute" in str(refusal.value)
    assert path.read_bytes() == before
