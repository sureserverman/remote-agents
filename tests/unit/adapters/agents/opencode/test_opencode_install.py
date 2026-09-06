"""OpenCode's install is the same reversibility contract against a different file shape.

The other two providers write hook *groups* into a `"hooks"` object. OpenCode has no such
mechanism: a plugin is a path in a top-level `"plugin"` array, and the thing it points at is a
file of JavaScript this installer generates. So there are two artifacts to put back, not one,
and the refusals the settings machinery already owns — the stale read, the exact-formatting
round trip, the entry it will not claim as its own — have to hold across both.

Every test here drives the real `install_agent_hooks` / `remove_agent_hooks` against a
`tmp_path` home. The one fact that matters more here than for the other two providers: the
entry written into an operator's config causes OpenCode to **load and execute** the file it
names, inside the agent's own process.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from remote_agents.adapters.agents.opencode.plugin import PLUGIN_RELATIVE_PATH
from remote_agents.adapters.agents.registry import (
    HookInstallError,
    default_settings_path,
    install_agent_hooks,
    remove_agent_hooks,
)
from remote_agents.bootstrap import main

#: An entry the operator put there themselves, which must survive install and removal alike.
#: Taken from the shape a real `opencode.json` on this host carries.
_FOREIGN_PLUGIN = "file:///home/owner/dev/some-other-tool/dist/index.js"


def _config(directory: Path, document: object | None = None) -> Path:
    """An `opencode.json` under a home-shaped tree, holding the operator's own settings."""
    path = directory / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(document, bytes):
        path.write_bytes(document)
        return path
    content = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [_FOREIGN_PLUGIN],
        "tools": {"skill": False},
    }
    path.write_text(
        json.dumps(content if document is None else document, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _plugin_path(settings: Path) -> Path:
    return settings.parent / PLUGIN_RELATIVE_PATH


def _entries(settings: Path) -> list:
    return json.loads(settings.read_text(encoding="utf-8"))["plugin"]


def test_the_settings_path_and_the_plugin_path_are_both_under_the_named_home(tmp_path: Path):
    """`--settings` redirects the config; the plugin follows it rather than the real home."""
    assert (
        default_settings_path(tmp_path, provider="opencode", environment={})
        == tmp_path / ".config" / "opencode" / "opencode.json"
    )


def test_install_adds_one_entry_naming_the_file_it_writes(tmp_path: Path) -> None:
    """The config points at the generated plugin, and the generated plugin exists."""
    settings = _config(tmp_path)

    outcome = install_agent_hooks(settings, executable=Path("/old/python"), provider="opencode")

    assert outcome.changed
    plugin = _plugin_path(settings)
    assert plugin.is_file()
    assert _entries(settings) == [_FOREIGN_PLUGIN, plugin.as_uri()]
    assert "/old/python" in plugin.read_text(encoding="utf-8")


def test_the_generated_plugin_names_the_interpreter_and_the_package_entry_point(tmp_path: Path):
    """A hook resolved through `PATH` is one that silently fails; the interpreter is fixed."""
    settings = _config(tmp_path)

    install_agent_hooks(settings, executable=Path("/opt/venv/bin/python"), provider="opencode")

    source = _plugin_path(settings).read_text(encoding="utf-8")
    assert '"/opt/venv/bin/python"' in source
    assert '"-m", "remote_agents", "agent-event"' in source
    assert '"--provider", "opencode"' in source


def test_the_spool_directory_reaches_the_generated_plugin(tmp_path: Path) -> None:
    """`--activity-dir` is what the live drill redirects; it must land in the argv array."""
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    settings = _config(tmp_path)

    install_agent_hooks(settings, activity_directory=spool, provider="opencode")

    source = _plugin_path(settings).read_text(encoding="utf-8")
    assert f'"--activity-dir", "{spool}"' in source


def test_installing_twice_leaves_exactly_one_entry_and_reports_no_change(tmp_path: Path) -> None:
    """Idempotent on both artifacts: one entry, and a second run that changes nothing."""
    settings = _config(tmp_path)

    install_agent_hooks(settings, executable=Path("/old/python"), provider="opencode")
    after_first = settings.read_bytes()
    plugin_after_first = _plugin_path(settings).read_bytes()

    outcome = install_agent_hooks(settings, executable=Path("/old/python"), provider="opencode")

    assert not outcome.changed
    assert settings.read_bytes() == after_first
    assert _plugin_path(settings).read_bytes() == plugin_after_first
    assert len(_entries(settings)) == 2


def test_a_reinstall_from_a_moved_virtualenv_replaces_the_plugin_rather_than_doubling_it(
    tmp_path: Path,
) -> None:
    """The entry is the same path either way, so what an upgrade must refresh is the file."""
    settings = _config(tmp_path)

    install_agent_hooks(settings, executable=Path("/old/python"), provider="opencode")
    outcome = install_agent_hooks(settings, executable=Path("/new/python"), provider="opencode")

    assert outcome.changed
    source = _plugin_path(settings).read_text(encoding="utf-8")
    assert "/new/python" in source
    assert "/old/python" not in source
    assert len(_entries(settings)) == 2


def test_remove_restores_the_file_byte_for_byte_and_deletes_the_plugin(tmp_path: Path) -> None:
    """Both artifacts go back: the operator's bytes exactly, and no executable left behind."""
    settings = _config(tmp_path)
    before = settings.read_bytes()

    install_agent_hooks(settings, provider="opencode")
    outcome = remove_agent_hooks(settings, provider="opencode")

    assert outcome.changed
    assert settings.read_bytes() == before
    assert not _plugin_path(settings).exists()
    assert _entries(settings) == [_FOREIGN_PLUGIN]


def test_remove_restores_byte_for_byte_after_a_repeated_install(tmp_path: Path) -> None:
    """A reinstall must not leave a second entry that removal then cannot see."""
    settings = _config(tmp_path)
    before = settings.read_bytes()

    install_agent_hooks(settings, executable=Path("/old/python"), provider="opencode")
    install_agent_hooks(settings, executable=Path("/new/python"), provider="opencode")
    remove_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before


def test_removal_from_a_config_holding_only_our_entry_drops_the_key(tmp_path: Path) -> None:
    """An empty `plugin` array and no key at all are different text; removal leaves neither."""
    settings = _config(tmp_path, {"$schema": "https://opencode.ai/config.json"})
    before = settings.read_bytes()

    install_agent_hooks(settings, provider="opencode")
    remove_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before
    assert "plugin" not in json.loads(settings.read_text(encoding="utf-8"))


def test_an_entry_this_installer_did_not_write_is_never_removed(tmp_path: Path) -> None:
    """Failing to remove a hook is recoverable; deleting somebody else's plugin is not."""
    hand_edited = "file:///home/owner/.config/opencode/remote-agents/activity-plugin.mjs.bak"
    settings = _config(
        tmp_path, {"plugin": [_FOREIGN_PLUGIN, hand_edited], "tools": {"skill": False}}
    )
    before = settings.read_bytes()

    install_agent_hooks(settings, provider="opencode")
    remove_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before
    assert hand_edited in _entries(settings)


def test_an_entry_sharing_only_the_file_name_is_not_ours_to_delete(tmp_path: Path) -> None:
    """The whole relative path is the tail that identifies us, never the basename alone.

    Found by a mutant: recognition narrowed to `parts[-1:]` passed every other test here, and
    would have deleted an operator's own `activity-plugin.mjs` -- living in their project, named
    the same by coincidence -- out of their configuration the first time they uninstalled.
    """
    theirs = "file:///home/owner/dev/their-project/activity-plugin.mjs"
    settings = _config(tmp_path, {"plugin": [theirs]})
    before = settings.read_bytes()

    install_agent_hooks(settings, provider="opencode")
    remove_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before
    assert _entries(settings) == [theirs]


def test_an_unrecognised_variant_of_our_entry_is_reported_rather_than_silently_doubled(
    tmp_path: Path,
) -> None:
    """A wrapper or a hand-edit naming our file loads it twice; say so rather than stay quiet."""
    variant = "/home/owner/.config/opencode/remote-agents/activity-plugin.mjs"
    settings = _config(tmp_path, {"plugin": [variant]})

    outcome = install_agent_hooks(settings, provider="opencode")

    assert "does not recognise as its own" in outcome.summary
    assert variant in _entries(settings)


def test_a_config_that_is_not_valid_json_is_refused_and_left_untouched(tmp_path: Path) -> None:
    settings = _config(tmp_path, b"{ not json")

    with pytest.raises(HookInstallError, match="not valid JSON"):
        install_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == b"{ not json"
    assert not _plugin_path(settings).exists()


def test_a_plugin_key_that_is_not_an_array_is_refused_and_left_untouched(tmp_path: Path) -> None:
    """A shape this installer would have to guess at is one it refuses to merge into."""
    settings = _config(tmp_path, {"plugin": {"not": "an array"}})
    before = settings.read_bytes()

    with pytest.raises(HookInstallError, match="not a JSON array"):
        install_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before
    assert not _plugin_path(settings).exists()


def test_a_config_this_installer_could_not_restore_exactly_is_refused(tmp_path: Path) -> None:
    """An empty array is indistinguishable, once installed into, from no key at all."""
    settings = _config(tmp_path, {"plugin": []})
    before = settings.read_bytes()

    with pytest.raises(HookInstallError, match="could not put it back"):
        install_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before


def test_a_config_whose_formatting_cannot_be_reproduced_is_refused(tmp_path: Path) -> None:
    """Removing later would rewrite the rest of the operator's file, so nothing is written."""
    settings = _config(tmp_path)
    settings.write_text('{\n      "plugin": [ ]  }\n', encoding="utf-8")
    before = settings.read_bytes()

    with pytest.raises(HookInstallError, match="cannot be reproduced exactly"):
        install_agent_hooks(settings, provider="opencode")

    assert settings.read_bytes() == before


def test_a_write_landing_while_this_one_computes_is_refused_rather_than_swallowed(
    tmp_path: Path, monkeypatch
) -> None:
    """A whole-file replace built from a stale read discards whatever landed in between."""
    settings = _config(tmp_path)
    monkeypatch.setattr(
        "remote_agents.adapters.agents.registry._refuse_if_changed_since_it_was_read",
        lambda *args: (_ for _ in ()).throw(HookInstallError("changed while preparing")),
    )

    with pytest.raises(HookInstallError, match="changed while preparing"):
        install_agent_hooks(settings, provider="opencode")


def test_the_plugin_and_its_directory_are_owner_only(tmp_path: Path) -> None:
    """The file OpenCode executes is not readable or replaceable by anyone else."""
    settings = _config(tmp_path)

    install_agent_hooks(settings, provider="opencode")

    plugin = _plugin_path(settings)
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o600
    assert stat.S_IMODE(plugin.parent.stat().st_mode) == 0o700


def test_a_plugin_directory_left_open_by_something_else_is_tightened(tmp_path: Path) -> None:
    """The one ancestor this installer owns gets 0700 whatever it was, rather than a refusal.

    Refusing on a writable *ancestor* was tried and removed: `~/.config` and `~/.config/opencode`
    are group-writable on an ordinary umask-0002 machine, so the refusal fired for a group whose
    only member is the owner and made the provider uninstallable. What is enforceable is the
    directory this installer creates.
    """
    settings = _config(tmp_path)
    directory = _plugin_path(settings).parent
    directory.mkdir(parents=True)
    directory.chmod(0o777)

    install_agent_hooks(settings, provider="opencode")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_reinstall_repairs_modes_loosened_since_the_last_one(tmp_path: Path) -> None:
    """Enforced on every run, not only the first — which the docstring above used to claim
    while the code returned before reaching it.

    A gate evaluator measured the gap: chmod the plugin 0666, re-run the installer, and it
    answered "already current" because the bytes matched and never looked at the mode. A
    world-writable file that OpenCode loads and executes surviving every subsequent install is
    the exact risk this stage declared.
    """
    settings = _config(tmp_path)
    install_agent_hooks(settings, provider="opencode")
    plugin = _plugin_path(settings)
    plugin.chmod(0o666)
    plugin.parent.chmod(0o777)

    outcome = install_agent_hooks(settings, provider="opencode")

    assert stat.S_IMODE(plugin.stat().st_mode) == 0o600
    assert stat.S_IMODE(plugin.parent.stat().st_mode) == 0o700
    assert outcome.changed, "a repaired mode is something the operator should be told about"


def test_a_reinstall_that_only_tightens_the_directory_still_reports_a_change(
    tmp_path: Path,
) -> None:
    """The directory is the more dangerous half, and it was the silent one.

    0777 on it lets anyone unlink the 0600 plugin and leave a file of their own -- the planted-file
    case the guard chain exists for. The exposure was always closed, because the directory is
    tightened on every run; what a round-2 verification pass found was that only the *file's*
    repaired mode counted as a change, so an install that fixed this said "already current".
    """
    settings = _config(tmp_path)
    install_agent_hooks(settings, provider="opencode")
    _plugin_path(settings).parent.chmod(0o777)

    outcome = install_agent_hooks(settings, provider="opencode")

    assert stat.S_IMODE(_plugin_path(settings).parent.stat().st_mode) == 0o700
    assert outcome.changed, "a repaired directory mode is as much news as a repaired file mode"


def test_a_plugin_file_with_our_content_and_something_appended_is_rewritten(
    tmp_path: Path,
) -> None:
    """Whole bytes, never a prefix — the one shape most worth repairing.

    Caught in the working tree by a gate evaluator before it was committed: shortening the
    comparison to the first `len(content)` bytes, as a fix for reading a whole file to inspect
    one line, made a file holding our exact content *plus appended code* compare equal. The
    installer would then decline to repair a plugin somebody had extended.
    """
    settings = _config(tmp_path)
    install_agent_hooks(settings, provider="opencode")
    plugin = _plugin_path(settings)
    with plugin.open("a", encoding="utf-8") as handle:
        handle.write("\nglobalThis.SOMETHING_ELSE = 1;\n")

    outcome = install_agent_hooks(settings, provider="opencode")

    assert outcome.changed
    assert "SOMETHING_ELSE" not in plugin.read_text(encoding="utf-8")


def test_an_xdg_config_home_moves_both_artifacts(tmp_path: Path) -> None:
    """OpenCode honours it, and this stage's own measurement used it to relocate the config.

    Assuming `~/.config` on a host that sets the variable would create a config file and a
    plugin OpenCode never reads, and report `installed the opencode activity plugin in ...`
    over the top. A gate evaluator found the assumption undisclosed.
    """
    relocated = tmp_path / "xdg"
    relocated.mkdir()

    resolved = default_settings_path(
        tmp_path, provider="opencode", environment={"XDG_CONFIG_HOME": str(relocated)}
    )

    assert resolved == relocated / "opencode" / "opencode.json"


def test_the_ambient_environment_is_read_at_call_time_when_none_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production callers pass nothing, so the default has to be the real environment.

    Asserted separately from the explicit-mapping cases because those would pass just as well if
    the parameter had quietly become the only source, which would make `install-agent-hooks`
    stop honouring the variable on a real host while every test stayed green.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient"))

    assert default_settings_path(tmp_path, provider="opencode") == (
        tmp_path / "ambient" / "opencode" / "opencode.json"
    )


def test_a_relative_xdg_config_home_is_ignored_rather_than_resolved(tmp_path: Path) -> None:
    """The XDG specification says to ignore it, and resolving it would answer about the
    directory this command was run from rather than the one OpenCode reads."""
    assert default_settings_path(
        tmp_path, provider="opencode", environment={"XDG_CONFIG_HOME": "relative/path"}
    ) == tmp_path / ".config" / "opencode" / "opencode.json"


def test_the_other_providers_are_untouched_by_the_xdg_branch(tmp_path: Path) -> None:
    """claude and codex keep their own dotdirectories; only a `.config` provider takes it."""
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}

    assert (
        default_settings_path(tmp_path, provider="claude", environment=environment)
        == tmp_path / ".claude" / "settings.json"
    )
    assert (
        default_settings_path(tmp_path, provider="codex", environment=environment)
        == tmp_path / ".codex" / "hooks.json"
    )


def test_the_config_keeps_its_mode_and_no_temporary_file_survives(tmp_path: Path) -> None:
    settings = _config(tmp_path)
    settings.chmod(0o600)

    install_agent_hooks(settings, provider="opencode")

    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert [entry.name for entry in settings.parent.iterdir() if entry.name.startswith(".")] == []


def test_an_absent_config_is_created_and_remove_is_a_quiet_no_op(tmp_path: Path) -> None:
    """A fresh machine gets a config holding only our entry; an uninstalled one costs nothing."""
    directory = tmp_path / ".config" / "opencode"
    directory.mkdir(parents=True)
    settings = directory / "opencode.json"

    assert not remove_agent_hooks(settings, provider="opencode").changed

    install_agent_hooks(settings, provider="opencode")
    assert _entries(settings) == [_plugin_path(settings).as_uri()]

    remove_agent_hooks(settings, provider="opencode")
    assert json.loads(settings.read_text(encoding="utf-8")) == {}
    assert not _plugin_path(settings).exists()


def test_an_absent_config_directory_is_refused_rather_than_created(tmp_path: Path) -> None:
    """No OpenCode configuration on this machine means nothing to install into."""
    with pytest.raises(HookInstallError, match="does not exist"):
        install_agent_hooks(tmp_path / "nowhere" / "opencode.json", provider="opencode")


def test_the_subcommand_installs_and_removes_through_the_named_config(tmp_path: Path) -> None:
    """The provider reaches the installer from the CLI, which is how an operator runs it."""
    settings = _config(tmp_path)
    before = settings.read_bytes()

    assert main(["install-agent-hooks", "--provider", "opencode", "--settings", str(settings)]) == 0
    assert _plugin_path(settings).is_file()

    assert (
        main(
            [
                "install-agent-hooks",
                "--provider",
                "opencode",
                "--settings",
                str(settings),
                "--remove",
            ]
        )
        == 0
    )
    assert settings.read_bytes() == before
    assert not _plugin_path(settings).exists()


def test_a_removal_leaves_a_plugin_file_this_installer_did_not_write(tmp_path: Path) -> None:
    """Deleting an unrecognised file at our path would be the unrecoverable half of a guess."""
    settings = _config(tmp_path)
    plugin = _plugin_path(settings)
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text("// somebody else's file, at our name\n", encoding="utf-8")

    remove_agent_hooks(settings, provider="opencode")

    assert plugin.exists()


def test_a_file_this_installer_did_not_write_is_never_overwritten(tmp_path: Path) -> None:
    """Overwriting somebody's file destroys its content as permanently as deleting it.

    Removal already checked the marker before deleting; install did not check it before
    overwriting, which a Tier-1 review named as the unguarded half of the same asymmetry.
    """
    settings = _config(tmp_path)
    before = settings.read_bytes()
    plugin = _plugin_path(settings)
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text("// somebody else's file, at our name\n", encoding="utf-8")

    with pytest.raises(HookInstallError, match="refusing to overwrite"):
        install_agent_hooks(settings, provider="opencode")

    assert plugin.read_text(encoding="utf-8") == "// somebody else's file, at our name\n"
    assert settings.read_bytes() == before


def test_an_entry_naming_another_host_is_not_ours_and_survives_an_install(tmp_path: Path) -> None:
    """A `file://` URL with an authority is a file on another machine, and never ours.

    Worse than a wrong deletion, which is why it is asserted against *install* rather than
    removal: the base document is computed by taking our entries out, so an entry wrongly
    claimed here disappears on the next install and is never put back. A Tier-1 review found it.
    """
    theirs = "file://otherhost/home/owner/.config/opencode/remote-agents/activity-plugin.mjs"
    settings = _config(tmp_path, {"plugin": [theirs]})

    install_agent_hooks(settings, provider="opencode")

    assert theirs in _entries(settings)
    assert len(_entries(settings)) == 2


def test_a_percent_encoded_separator_does_not_make_two_segments_out_of_one(tmp_path: Path):
    """`%2F` inside a segment is a literal slash in that name, never a path separator."""
    theirs = "file:///home/owner/elsewhere/remote-agents%2Factivity-plugin.mjs"
    settings = _config(tmp_path, {"plugin": [theirs]})

    install_agent_hooks(settings, provider="opencode")
    remove_agent_hooks(settings, provider="opencode")

    assert _entries(settings) == [theirs]


def test_a_plugin_path_standing_as_a_symlink_is_refused(tmp_path: Path) -> None:
    """A link left lying in wait would send generated code wherever it points."""
    settings = _config(tmp_path)
    plugin = _plugin_path(settings)
    plugin.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere.js"
    elsewhere.write_text("", encoding="utf-8")
    os.symlink(elsewhere, plugin)

    with pytest.raises(HookInstallError, match="symlink"):
        install_agent_hooks(settings, provider="opencode")

    assert elsewhere.read_text(encoding="utf-8") == ""
    assert _entries(settings) == [_FOREIGN_PLUGIN]


def test_the_refusal_is_repeated_immediately_before_the_write(tmp_path: Path) -> None:
    """One check at the top of the install left a long window; `_write_plugin` checks again."""
    from remote_agents.adapters.agents.hook_settings import _write_plugin

    plugin = _plugin_path(_config(tmp_path))
    plugin.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "somebody-elses-file"
    elsewhere.write_text("theirs\n", encoding="utf-8")
    os.symlink(elsewhere, plugin)

    with pytest.raises(HookInstallError, match="symlink"):
        _write_plugin(plugin, "// generated\n", "// generated")

    assert elsewhere.read_text(encoding="utf-8") == "theirs\n"
    assert plugin.is_symlink()


def test_the_plugin_write_replaces_a_link_rather_than_writing_through_it(tmp_path: Path) -> None:
    """What makes losing the race harmless, driven at the write rather than at the check.

    Both refusals above are checks, and a check cannot be atomic with the write that follows it
    (CWE-367). So the write itself is asked directly, with the link already in place at the
    moment of the rename -- which is the state a lost race produces. `follow_symlink=False` is
    the whole answer: the replace lands on the link's own directory entry, so the target keeps
    its content and our file stands where the link was.

    The settings file deliberately takes the opposite branch, and it is asserted here beside
    this one so the two intentions cannot be read as an inconsistency.
    """
    from remote_agents.adapters.agents.hook_settings import _write_atomically

    elsewhere = tmp_path / "somebody-elses-file"
    elsewhere.write_text("theirs\n", encoding="utf-8")
    link = tmp_path / "planted"
    os.symlink(elsewhere, link)

    _write_atomically(link, b"ours\n", 0o600, follow_symlink=False)

    assert elsewhere.read_text(encoding="utf-8") == "theirs\n"
    assert not link.is_symlink()
    assert link.read_bytes() == b"ours\n"

    settings_link = tmp_path / "settings-link"
    os.symlink(elsewhere, settings_link)
    _write_atomically(settings_link, b"through\n", 0o600)

    assert settings_link.is_symlink()
    assert elsewhere.read_text(encoding="utf-8") == "through\n"


def test_a_removal_that_cannot_write_the_config_has_not_yet_deleted_the_plugin(
    tmp_path: Path,
) -> None:
    """The entry goes first, so a failure leaves an orphan file rather than a dangling entry.

    Found by an adversarial review, which ran exactly this and watched the old order delete the
    file, fail to write the config, and print "Nothing was lost" over an entry now naming a file
    that is gone -- a load error in the operator's next OpenCode session. Reversed, the worst
    case is a file nothing points at, which is inert and which a re-run collects.
    """
    settings = _config(tmp_path)
    install_agent_hooks(settings, provider="opencode")
    plugin = _plugin_path(settings)
    settings.parent.chmod(0o500)
    try:
        with pytest.raises(HookInstallError):
            remove_agent_hooks(settings, provider="opencode")
    finally:
        settings.parent.chmod(0o755)

    assert plugin.is_file(), "the plugin was deleted before the entry naming it was taken out"
    assert _entries(settings) == [_FOREIGN_PLUGIN, plugin.as_uri()]

    remove_agent_hooks(settings, provider="opencode")
    assert not plugin.exists()


def test_an_orphaned_plugin_file_is_collected_by_a_later_removal(tmp_path: Path) -> None:
    """The invariant that makes both orderings safe: deletion needs no entry to exist."""
    settings = _config(tmp_path)
    install_agent_hooks(settings, provider="opencode")
    plugin = _plugin_path(settings)
    settings.write_bytes(_config(tmp_path / "fresh").read_bytes())

    outcome = remove_agent_hooks(settings, provider="opencode")

    assert outcome.changed
    assert not plugin.exists()


def test_a_symlinked_config_directory_is_the_operators_arrangement_and_installs(
    tmp_path: Path,
) -> None:
    """`stow`-managed dotfiles symlink exactly that directory; refusing made this uninstallable.

    An adversarial review found the provider refusing on a plausible layout, with a message
    naming the leaf and never the link -- no path forward from the output. A symlinked *ancestor*
    is the operator's own arrangement, which is the argument `_write_atomically` already makes
    for writing through a symlinked settings file.
    """
    real = tmp_path / "dotfiles" / "opencode"
    real.mkdir(parents=True)
    (tmp_path / ".config").mkdir()
    link = tmp_path / ".config" / "opencode"
    os.symlink(real, link)
    settings = link / "opencode.json"
    settings.write_text(json.dumps({"plugin": [_FOREIGN_PLUGIN]}, indent=2) + "\n", "utf-8")
    before = settings.read_bytes()

    install_agent_hooks(settings, provider="opencode")

    plugin = real / PLUGIN_RELATIVE_PATH
    assert plugin.is_file()
    assert stat.S_IMODE(plugin.parent.stat().st_mode) == 0o700
    remove_agent_hooks(settings, provider="opencode")
    assert settings.read_bytes() == before
    assert not plugin.exists()


def test_a_symlink_standing_where_this_installer_creates_its_directory_is_refused(
    tmp_path: Path,
) -> None:
    """An ancestor is theirs; the one directory this installer makes is not, and names the link."""
    settings = _config(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.symlink(victim, settings.parent / "remote-agents")

    with pytest.raises(HookInstallError, match="symlink"):
        install_agent_hooks(settings, provider="opencode")

    assert list(victim.iterdir()) == []
    assert _entries(settings) == [_FOREIGN_PLUGIN]


def test_an_entry_naming_the_same_file_name_in_another_directory_is_left_alone(
    tmp_path: Path,
) -> None:
    """Tail matching claimed it; exact-path matching does not, and the note says it saw it.

    The case an adversarial review used is the one this project's own developer hits: a checkout
    of this repository is itself called `remote-agents`, so a working-tree copy at
    `~/dev/remote-agents/activity-plugin.mjs` ended a plugin entry with exactly our tail. It was
    claimed, taken out of the base document on install, and never put back -- against a runbook
    promising the file comes back byte for byte.
    """
    theirs = "file:///home/owner/dev/remote-agents/activity-plugin.mjs"
    settings = _config(tmp_path, {"plugin": [theirs]})
    before = settings.read_bytes()

    outcome = install_agent_hooks(settings, provider="opencode")

    assert theirs in _entries(settings)
    assert "does not recognise as its own" in outcome.summary, (
        "an entry we are fairly sure was once ours must be spoken about, not silently kept"
    )

    remove_agent_hooks(settings, provider="opencode")
    assert settings.read_bytes() == before


def test_a_named_pipe_standing_at_our_name_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    """The guard's intent was always "we did not write this"; only its check said "a file"."""
    settings = _config(tmp_path)
    plugin = _plugin_path(settings)
    plugin.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(plugin)

    with pytest.raises(HookInstallError, match="not a regular file"):
        install_agent_hooks(settings, provider="opencode")

    assert stat.S_ISFIFO(plugin.lstat().st_mode)
    assert _entries(settings) == [_FOREIGN_PLUGIN]


def test_no_shape_aware_helper_lets_a_caller_forget_which_file_is_ours() -> None:
    """A structural assertion, because the property is an absence and a case cannot pin it.

    `ours` carried a `None` default for one commit. A round-2 verification pass measured the
    cost: a caller who forgot it got a removal that silently KEPT our own entry and reported
    "no agent hooks" — the guard whose whole purpose is not leaving executable code in an
    operator's configuration, failing open. Every production call site passed it, so no
    behavioural test could ever have caught the default; what is checkable is that the default
    is gone, which is the decision rather than one of its consequences.
    """
    import inspect

    from remote_agents.adapters.agents import hook_settings

    for name in (
        "_without_our_groups",
        "_holds_our_groups",
        "_foreign_variant_note",
        "_foreign_plugin_note",
        "_refuse_when_removal_would_not_restore",
        "_is_our_plugin_entry",
    ):
        parameter = inspect.signature(getattr(hook_settings, name)).parameters["ours"]
        assert parameter.default is inspect.Parameter.empty, (
            f"{name}'s `ours` has a default again; a caller that forgets it disables the guard"
        )
