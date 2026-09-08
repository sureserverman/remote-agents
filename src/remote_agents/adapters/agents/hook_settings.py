"""Provider-neutral settings-file machinery: read, validate, restyle, write atomically.

Extracted from the retired `hook_install.py` ahead of the provider split — shared machinery
asked, not copied (the principle DEC-043's title records; the entry itself is about use-case
decisions, and this extraction is the verticals plan's): what varies per
provider is *which* file and *which* events — the `_HookProvider` values — while everything
here is about editing an operator's JSON settings file reversibly, whoever owns it. The
formatting-recovery contract (`_detected_style`), the stale-read refusal and the atomic
replace move whole; the retired installer's module docstring — the design record for
why each refusal exists — is kept verbatim beside the install surface in `registry.py`.
Names keep their underscores because they moved, not changed.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from remote_agents.ports.private_directory import ancestors_writable_by_others


class HookInstallError(Exception):
    """A settings file this installer will not write to, and the reason why."""


@dataclass(frozen=True, slots=True)
class _PluginEntry:
    """The second settings shape: a list of module paths, not an object of hook groups.

    A provider declaring this one is saying its agent has no hook-command mechanism at all --
    the way in is a file of code the config names, which the agent then loads into its own
    process. Everything else about the install is unchanged, which is the point of naming the
    difference as data: the stale read, the exact-formatting round trip, the entry this
    installer will not claim and the atomic replace are all shared with the other shape.

    `render` turns the spool command's argv into the file's text, and lives with the provider
    because the language it emits is that provider's business (ARCH-02). `marker` is the first
    line that text always carries, and is what removal checks before deleting anything.
    """

    key: str
    relative_path: Path
    """Where the generated file goes, relative to the directory holding the settings file.

    Relative so that a redirected `--settings` -- a drill, a test, an `XDG_CONFIG_HOME` -- moves
    both artifacts together; the absolute path is computed per install from the settings file's
    own directory.

    **It is not matched as a tail.** An earlier version of this docstring said it was, by analogy
    with `_COMMAND_TAIL`, and that analogy was wrong for a path: `_is_our_plugin_entry` compares
    the whole absolute path -- *unresolved* on both sides, so a symlinked config directory is
    compared as the operator wrote it rather than as the filesystem resolves it -- because a
    checkout of this project is itself called `remote-agents`
    and the two-segment tail therefore matched an operator's own working-tree copy. A round-2
    verification pass found this paragraph still standing after the code beneath it had changed
    -- on the field's own definition, which is the first place a maintainer looks.
    """

    marker: str
    render: Callable[[list[str]], str]


@dataclass(frozen=True, slots=True)
class _HookProvider:
    name: str
    configuration_relative_path: Path
    installed_events: tuple[str, ...]
    retired_events: tuple[str, ...] = ()
    plugin: _PluginEntry | None = None
    """The plugin shape, or `None` for a provider whose agent takes hook commands.

    Data rather than an identity check, exactly as `flagless` is: this module names no provider,
    and the instances live with the installer.
    """

    flagless: bool = False
    """Whether this provider's hook commands omit `--provider <name>`.

    Claude's commands predate the option and stay flagless so a reinstall replaces the
    existing entry instead of adding a second; every later provider carries the flag. Data
    on the value rather than an identity check against the claude instance, so this shared
    machinery names no provider (the instances live with the installer).
    """


@dataclass(frozen=True, slots=True)
class _SettingsStyle:
    """One way of turning a document back into text, recovered from the file's own bytes."""

    indent: int | str | None
    separators: tuple[str, str]
    ensure_ascii: bool
    trailing_newline: bool

    def render(self, document: Any) -> bytes:
        text = json.dumps(
            document,
            indent=self.indent,
            separators=self.separators,
            ensure_ascii=self.ensure_ascii,
        )
        return f"{text}\n".encode() if self.trailing_newline else text.encode()


# What a file created from nothing gets: two-space indentation and a trailing newline, which
# is what the agent's own writer produces and what a hand-edit expects to find.
_DEFAULT_STYLE = _SettingsStyle(2, (",", ": "), ensure_ascii=False, trailing_newline=True)


@dataclass(frozen=True, slots=True)
class _Settings:
    """A settings file as read: its bytes, its document, its formatting and its mode."""

    path: Path
    content: bytes | None
    document: dict[str, Any]
    style: _SettingsStyle
    mode: int


def _read_settings(path: Path, provider: _HookProvider) -> _Settings:
    """Parse and validate a settings file, refusing every shape that cannot be merged into."""
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise HookInstallError(
                f"{path.parent} does not exist, so this machine has no agent configuration to "
                "install into; refusing to create one"
            ) from None
        # A settings file is the agent's, and creating one holding only our own hooks is both
        # valid and what a fresh machine needs. Removal later empties it back to `{}` rather
        # than deleting it, because by then the file may hold settings we never saw.
        return _Settings(path, None, {}, _DEFAULT_STYLE, 0o600)
    except OSError as error:
        raise HookInstallError(f"cannot read {path}: {error}") from error
    try:
        document = json.loads(content)
    except ValueError as error:
        raise HookInstallError(
            f"{path} is not valid JSON ({error}); it has been left untouched"
        ) from error
    if not isinstance(document, dict):
        raise HookInstallError(f"{path} does not hold a JSON object; it has been left untouched")
    _refuse_unmergeable_hooks(path, document, provider)
    return _Settings(
        path,
        content,
        document,
        _detected_style(path, document, content),
        stat.S_IMODE(path.stat().st_mode),
    )


def _refuse_unmergeable_hooks(
    path: Path, document: dict[str, Any], provider: _HookProvider
) -> None:
    """Reject a settings shape this installer would have to guess at, either kind."""
    if provider.plugin is not None:
        entries = document.get(provider.plugin.key)
        if entries is not None and not isinstance(entries, list):
            raise HookInstallError(
                f'the "{provider.plugin.key}" key in {path} is not a JSON array; it has been '
                "left untouched"
            )
        return
    hooks = document.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise HookInstallError(
            f'the "hooks" key in {path} is not a JSON object; it has been left untouched'
        )
    for event in (*provider.installed_events, *provider.retired_events):
        groups = hooks.get(event)
        if groups is not None and not isinstance(groups, list):
            raise HookInstallError(
                f'"hooks.{event}" in {path} is not a JSON array; it has been left untouched'
            )


def _detected_style(path: Path, document: dict[str, Any], content: bytes) -> _SettingsStyle:
    """Find the formatting that reproduces this file exactly, or refuse to rewrite it.

    Reproducing the untouched document is the whole test: a style that returns the original
    bytes for the original document will also return the original bytes for that document
    once our groups are taken back out. Nothing here guesses from the text, because a guess
    that is nearly right would reformat the operator's file on the way past.
    """
    for style in _candidate_styles():
        if style.render(document) == content:
            return style
    raise HookInstallError(
        f"the formatting of {path} cannot be reproduced exactly, so removing these hooks "
        "later would rewrite the rest of the file; it has been left untouched"
    )


def _candidate_styles() -> Iterator[_SettingsStyle]:
    """Enumerate the renderings a JSON writer plausibly produced, likeliest first."""
    for indent in (2, 4, None, 1, 3, 8, "\t"):
        separators = (
            ((", ", ": "), (",", ":"), (",", ": ")) if indent is None else ((",", ": "), (",", ":"))
        )
        for separator in separators:
            for ensure_ascii in (False, True):
                for trailing_newline in (True, False):
                    yield _SettingsStyle(indent, separator, ensure_ascii, trailing_newline)


def _persist_directory_entry(directory: Path) -> None:
    """Make the rename itself durable, not just the bytes it renamed.

    The content is fsynced before the replace, but the *entry* naming it is not, so a crash
    straight after a successful install could leave the settings file at its pre-install
    content while the command has already reported success. Best effort: the replacement is
    visible to every reader by this point, so failing here would report that nothing was
    written about a change that in fact landed. Same reasoning, and the same shape, as
    `registry_writer._sync_directory`.
    """
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _refuse_if_changed_since_it_was_read(path: Path, expected: bytes | None) -> None:
    """Check the bytes are still the ones this change was computed against.

    A whole-file replace built from a stale read discards whatever landed in between, and the
    agent whose settings these are writes them itself -- a model change, an "always allow"
    grant -- while this command is plausibly being run from inside one of its sessions. The
    window is milliseconds and the loss is silent and total, which is the combination worth a
    check rather than a comment.

    Not a lock: two writers can still interleave inside the moment between this read and the
    rename below. It converts the likely case, a write that landed while this process was
    parsing and rendering, from silent loss into a refusal the operator can act on.
    """
    try:
        current = path.read_bytes() if path.exists() else None
    except OSError as error:
        raise HookInstallError(f"cannot re-read {path}: {error}") from error
    if current != expected:
        raise HookInstallError(
            f"{path} changed while this command was preparing its edit, so it has been left "
            "untouched rather than written from what it used to say. Nothing was lost — run "
            "the command again."
        )


def _write_atomically(
    path: Path, content: bytes, mode: int, *, follow_symlink: bool = True
) -> None:
    """Replace the file whole, so an interruption can never leave a half-written settings file.

    The temporary lands beside the *resolved* file to keep the rename within one filesystem,
    and ``mkstemp`` opens it owner-only, so the window in which the new content exists under a
    guessable name never happens at all.

    Resolving first is what makes a symlinked settings file keep being one. `os.replace` acts
    on the directory entry, not on what it points at, so renaming onto the link would quietly
    turn it into a regular file and strand the real file it came from -- a plausible outcome
    for anyone whose dotfiles are symlinked into place, and a change to something this module
    promises to leave as it found it. Writing through the link edits the file the operator
    actually keeps.

    ``follow_symlink=False`` inverts exactly that, for the one file where the argument runs the
    other way: a generated plugin was never anywhere else, so a link standing at its name is a
    redirection rather than a preference. `_refuse_a_planted_plugin_path` reports one it can see,
    but the check and this write are separate syscalls and a same-user process can plant a link
    between them (CWE-367; a Tier-1 review found the window). Not resolving is what makes that
    race harmless rather than merely unlikely: the replace lands on the link's own directory
    entry, so the worst outcome is our regular file standing where the link was, and never our
    content written through it to a target somebody else chose.
    """
    if follow_symlink:
        path = path.resolve()
    try:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as error:
        # A refusal, like every other one here: the caller prints a line and exits non-zero
        # rather than showing a traceback. An unwritable directory and a full disk both land
        # here, and neither has touched the settings file, which mkstemp never opened.
        raise HookInstallError(f"cannot write beside {path}: {error}") from error
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _persist_directory_entry(path.parent)
    except BaseException as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, OSError):
            # The settings file is still whole -- nothing was written to it, only to the
            # temporary that has just been removed -- so this too is a refusal, not a crash.
            raise HookInstallError(f"cannot write {path}: {error}") from error
        raise


# What identifies a group as ours: the argument tail, matched as parsed words rather than as
# text. The interpreter in front of it may change -- a moved virtualenv should replace our
# entry, not add a second -- so the tail is what is compared and the head is ignored.
#
# It is not a substring search. A command that merely mentions this pair -- an operator's own
# hook echoing a reminder about it, a grep for it in an auditing script -- is somebody else's
# hook, and removing it would be exactly the unrecoverable deletion this module refuses to
# risk elsewhere. Matching parsed words means "mentions" and "runs" stop being the same thing.
_COMMAND_TAIL = ("-m", "remote_agents", "agent-event")
_ACTIVITY_DIRECTORY_OPTION = "--activity-dir"
_PROVIDER_OPTION = "--provider"


def _with_our_groups(
    document: dict[str, Any], ours: str, provider: _HookProvider
) -> dict[str, Any]:
    """Append our own entry, without disturbing any key's position.

    `ours` is what identifies this installation in the operator's file: the hook command for a
    provider that takes hook commands, and the plugin file's own URL for one that takes a
    plugin. One argument rather than two because the two shapes never coexist in one provider.
    """
    if provider.plugin is not None:
        entries = list(document.get(provider.plugin.key) or ())
        return {**document, provider.plugin.key: [*entries, ours]}
    command = ours
    hooks = dict(document.get("hooks") or {})
    for event in provider.installed_events:
        group = {"hooks": [{"type": "command", "command": command}]}
        # `or ()` rather than a `.get` default, because an explicit JSON null defeats the
        # default and unpacking it raised out through the CLI as a traceback. Reading null as
        # "no groups" is what the validator above already decided; the round-trip check then
        # refuses the install anyway, since dropping our groups again would leave the key
        # gone rather than null -- which is the same refusal an empty list gets, and the same
        # message tells the operator to delete it. Same form as `_holds_our_groups`.
        hooks[event] = [*(hooks.get(event) or ()), group]
    return {**document, "hooks": hooks}


def _without_our_groups(
    document: dict[str, Any], provider: _HookProvider, ours: Path | None
) -> dict[str, Any]:
    """Drop our groups, and the containers left holding nothing once they are gone.

    Every other event, and every other group under the events we do install into, is copied
    across untouched — including a group of somebody else's that happens to share
    ``SessionEnd`` with ours, which is the case the operator's real file presents.

    For a plugin provider the same rule applies one container over: every entry the operator put
    in the list stays, the key goes when nothing of theirs is left in it, and an entry naming
    our file in a form this installer did not write is theirs (`_is_our_plugin_entry`).
    """
    if provider.plugin is not None:
        # No default on `ours`, and this is why. It defaulted to `None` for one commit, and a
        # round-2 verification pass measured what a caller who forgot it got: `_is_our_plugin_entry`
        # matches nothing, so removal silently KEEPS our own entry and reports "no agent hooks" --
        # a guard whose whole purpose is not leaving executable code in an operator's config,
        # failing open. Every argument here is now one a caller has to supply on purpose.
        entries = document.get(provider.plugin.key)
        if not isinstance(entries, list):
            return document
        kept = [entry for entry in entries if not _is_our_plugin_entry(entry, provider, ours)]
        if kept:
            return {**document, provider.plugin.key: kept}
        return {key: value for key, value in document.items() if key != provider.plugin.key}
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return document
    remaining: dict[str, Any] = {}
    # Now *or ever* — see `RETIRED_EVENTS`. Sweeping only what is currently installed is what
    # would stand an event we dropped, in a file neither install nor uninstall would touch
    # again.
    ours = (*provider.installed_events, *provider.retired_events)
    for event, groups in hooks.items():
        if event not in ours or not isinstance(groups, list):
            remaining[event] = groups
            continue
        kept = [group for group in groups if not _is_our_group(group, provider)]
        if kept:
            remaining[event] = kept
    if remaining:
        return {**document, "hooks": remaining}
    return {key: value for key, value in document.items() if key != "hooks"}


def _refuse_a_spool_others_can_reach(activity_directory: Path | None) -> None:
    """Check the chosen spool before writing a command that will keep writing into it.

    `--activity-dir` makes this location operator-supplied, and the guard on the other end
    refuses to write *through* a planted link while deliberately leaving an existing
    ancestor's mode alone. That leaves a precondition which was documented and unenforced:
    under an ancestor others can write, the leaf can be unlinked and replaced with a link
    between one hook firing and the next.

    Refused here because this is the moment the path is chosen and the only one that can say
    so out loud. The hook fires constantly and must stay silent, so a check there would turn
    a misconfiguration into a spool that mysteriously stays empty forever.

    Nothing is checked when the flag is absent: the default lives under the state directory
    `ProductionPaths` already refuses to resolve through a symlink.
    """
    if activity_directory is None:
        return
    if not activity_directory.is_absolute():
        # Refused before the mode check, because a relative path makes that check answer about
        # the wrong directory rather than fail: it resolves against *this* process's working
        # directory, while the path is embedded in the hook command verbatim and every Claude
        # session resolves it against its own. One spool per project, none of them the one
        # that was inspected, none of them the one the service drains -- and the check reports
        # safe throughout, which is worse than never having run.
        raise HookInstallError(
            f"--activity-dir must be an absolute path; {activity_directory} would mean a "
            "different directory in every project the agent runs in, and none of them the "
            "one this service reads."
        )
    exposed = ancestors_writable_by_others(activity_directory)
    if not exposed:
        return
    listed = ", ".join(str(parent) for parent in exposed)
    raise HookInstallError(
        f"refusing to install hooks that would spool into {activity_directory}, because "
        f"another user can write to {listed}. Anything able to write there can replace the "
        "spool with a link and read what your agents report. Choose a directory under your "
        "own home, or set the sticky bit on it as /tmp has."
    )


def _foreign_variant_note(base: dict[str, Any], provider: _HookProvider, ours: Path | None) -> str:
    """Name the events already running our subcommand in a form this installer will not manage.

    Leaving such an entry alone is the right call and stays the right call -- it is a wrapper,
    a hand-edit, or a future version, and removing it would be guessing about a command we did
    not write. But the consequence of leaving it was invisible: install adds its own group
    beside it, so the agent runs the hook twice for every one of those events, and `--remove`
    later takes only ours and leaves theirs spooling with nothing left that knows how to
    clean it up. Refusing outright would strand an operator who cannot install until they
    edit a file by hand; saying nothing left them with duplicate notifications and no clue
    where they came from. So it is reported, and the choice of what to do stays theirs.
    """
    if provider.plugin is not None:
        return _foreign_plugin_note(base, provider, ours)
    hooks = base.get("hooks")
    if not isinstance(hooks, dict):
        return ""
    events = [
        event
        for event in provider.installed_events
        if any(_mentions_our_subcommand(group, provider) for group in hooks.get(event) or ())
    ]
    if not events:
        return ""
    return (
        f". Note: {', '.join(events)} already runs this subcommand in a form this installer "
        "does not recognise and will not touch, so the hook now runs twice for those events; "
        "removing these hooks later will leave that entry in place"
    )


def _mentions_our_subcommand(group: Any, provider: _HookProvider) -> bool:
    """Report a group running our subcommand that `_is_our_group` will not claim."""
    if _is_our_group(group, provider) or not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            continue
        try:
            words = shlex.split(entry["command"])
        except ValueError:
            continue
        # The parsed words, exactly as `_runs_our_command` reads them -- so a command that
        # merely *mentions* the subcommand in an echo or a grep is no more a near-miss here
        # than it is one there.
        if tuple(words[1 : 1 + len(_COMMAND_TAIL)]) != _COMMAND_TAIL:
            continue
        tail = words[1 + len(_COMMAND_TAIL) :]
        prefix = [] if provider.flagless else [_PROVIDER_OPTION, provider.name]
        if tail[: len(prefix)] == prefix:
            return True
    return False


def _is_our_group(group: Any, provider: _HookProvider) -> bool:
    """Recognise a group this installer wrote, and never a group that merely resembles one.

    Every command in the group must be ours. A group an operator has hand-edited to run our
    command beside one of their own is therefore left alone: failing to remove a hook is
    recoverable, and deleting somebody else's is not.

    The group must also carry nothing but ``hooks``. This installer writes matcherless groups
    and adds no other key, so a group holding a ``matcher`` or a ``timeout`` is by
    construction not one it wrote -- and claiming it deleted an operator's deliberate
    narrowing on removal, or silently dropped it on reinstall, which is the same
    unrecoverable outcome the paragraph above exists to prevent.
    """
    if not isinstance(group, dict) or set(group) != {"hooks"}:
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list) or not entries:
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and _runs_our_command(entry["command"], provider)
        for entry in entries
    )


def _runs_our_command(command: str, provider: _HookProvider) -> bool:
    """Decide whether this command line is one this installer could have written.

    Not "mentions our subcommand", and not "invokes it somehow" either. The words after the
    interpreter must be our subcommand followed by nothing, or by the one option this
    installer knows how to add. An invocation carrying some other flag is something else's --
    a wrapper, a hand-edit, a future version -- and removing it would be guessing about a
    command we did not write.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        # Unbalanced quoting. Not something this installer wrote, so not ours to touch.
        return False
    if tuple(words[1 : 1 + len(_COMMAND_TAIL)]) != _COMMAND_TAIL:
        return False
    rest = words[1 + len(_COMMAND_TAIL) :]
    prefix = [] if provider.flagless else [_PROVIDER_OPTION, provider.name]
    if rest[: len(prefix)] != prefix:
        return False
    remaining = rest[len(prefix) :]
    return not remaining or (len(remaining) == 2 and remaining[0] == _ACTIVITY_DIRECTORY_OPTION)


def _refuse_when_removal_would_not_restore(
    settings: _Settings,
    base: dict[str, Any],
    installed: dict[str, Any],
    provider: _HookProvider,
    ours: Path | None,
) -> None:
    """Run the removal now and refuse the install unless it lands back on the original bytes.

    Two things can go wrong, and this catches both before anything is written. Removal might
    not undo the install — and removal might not be faithful to what is already on disk. The
    second is the reachable one: an empty ``"hooks": {}`` block is indistinguishable, once
    installed into, from a file that never had the key, so removal cannot know whether to
    leave it or delete it. Rather than pick and be wrong half the time, the install is
    refused; deleting the empty block by hand makes it succeed and changes nothing else.
    """
    restored = settings.style.render(_without_our_groups(installed, provider, ours))
    # On a reinstall the bytes on disk already hold our previous groups, so they are not what
    # removal must land on; the check that they were is the one the first install passed.
    faithful = (
        settings.content is None
        or _holds_our_groups(settings.document, provider, ours)
        or settings.style.render(base) == settings.content
    )
    if restored != settings.style.render(base) or not faithful:
        container = f'"{provider.plugin.key}": []' if provider.plugin is not None else '"hooks": {}'
        empty = (
            f'the empty "{provider.plugin.key}" array (or a null in its place)'
            if provider.plugin is not None
            else "the empty block (or the empty list, or a null, under any of "
            f"{', '.join((*provider.installed_events, *provider.retired_events))})"
        )
        raise HookInstallError(
            f"{settings.path} has been left untouched, because removing these hooks again "
            "could not put it back exactly as it is now. An empty container is almost always "
            f"the cause: {container} and no such key at all mean the same thing but are "
            "different text, so an uninstall cannot tell which one to leave behind. Delete "
            f"{empty} and run this again — that changes nothing else about your settings."
        )


def _holds_our_groups(document: dict[str, Any], provider: _HookProvider, ours: Path | None) -> bool:
    """Report whether a previous install is present, which is what makes this a reinstall."""
    if provider.plugin is not None:
        entries = document.get(provider.plugin.key)
        return isinstance(entries, list) and any(
            _is_our_plugin_entry(entry, provider, ours) for entry in entries
        )
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        _is_our_group(group, provider)
        for event in provider.installed_events
        for group in hooks.get(event) or ()
    )


def _foreign_plugin_note(base: dict[str, Any], provider: _HookProvider, ours: Path | None) -> str:
    """Name a plugin entry pointing at our file in a form this installer will not manage.

    The same call the hook shape makes, and it now carries a second population the hook shape has
    no equivalent of. One is the original: an entry naming this plugin in a spelling this
    installer would never write -- a bare path, another scheme -- which means OpenCode loads it
    twice. The other arrived when entry recognition narrowed from "ends with our tail" to "is
    exactly the file we would write": an entry naming a *different* file with our name, at
    another directory, is now correctly left alone rather than claimed, and the honest treatment
    of one is to say it was seen.

    Both are reported rather than refused, and neither is touched. Refusing outright would
    strand an operator who cannot install until they hand-edit a file; saying nothing would leave
    them with duplicate notifications, or a stale entry surviving an uninstall, and no clue where
    either came from. The choice stays theirs.
    """
    assert provider.plugin is not None
    entries = base.get(provider.plugin.key)
    if not isinstance(entries, list):
        return ""
    foreign = [
        entry
        for entry in entries
        if isinstance(entry, str)
        and provider.plugin.relative_path.name in entry
        and not _is_our_plugin_entry(entry, provider, ours)
    ]
    if not foreign:
        return ""
    return (
        f". Note: {', '.join(foreign)} already names a file called "
        f"{provider.plugin.relative_path.name} in a form this installer does not recognise as "
        "its own, so it is left exactly as it is -- by install, and by --remove later. If it is "
        "a second copy of this plugin, you are now notified twice and removing this one will not "
        "stop that; if it is yours, nothing here will touch it"
    )


def _is_our_plugin_entry(entry: Any, provider: _HookProvider, ours: Path | None) -> bool:
    """Recognise the entry this installer would write, and never one that merely resembles it.

    A `file://` URL naming **exactly** the file this install computes, `ours` -- not a file whose
    path merely ends the same way. The tail-matching version this replaces claimed any entry
    ending `remote-agents/activity-plugin.mjs` at any directory, and an adversarial review showed
    what that costs with a case this project's own developer hits: a working-tree checkout is
    itself called `remote-agents`, so an operator's entry naming a copy under `~/dev/` was
    claimed, taken out of the base document on install, and never put back. Silently, and
    against a runbook promising the file returns byte for byte.

    The head was ignored so that a *moved home* would still match. That case is not lost, only
    demoted from a silent claim to a spoken one: an entry ending our tail that is not `ours` is
    now reported by `_foreign_plugin_note`, which is the honest treatment of an entry we are
    fairly sure was ours and are not willing to delete on that.

    Every other spelling is somebody else's -- a bare path, another scheme, a relative entry, an
    npm specifier, an authority component, a query or a fragment. Removing one would be guessing.
    """
    return _plugin_entry_path(entry, provider) == ours if ours is not None else False


def _plugin_entry_path(entry: Any, provider: _HookProvider) -> Path | None:
    """The absolute path a plugin entry names, or `None` if it is not a plain local file URL."""
    if provider.plugin is None or not isinstance(entry, str):
        return None
    split = urlsplit(entry)
    # An authority is what `Path.as_uri()` never writes, and neither is a query or a fragment.
    # `file://otherhost/…` names a file on another machine.
    if split.scheme != "file" or split.netloc or split.query or split.fragment:
        return None
    if not split.path.startswith("/"):
        return None
    # Each segment is unquoted *after* the split, never before it. `%2F` inside a segment is a
    # literal slash in that segment's name, not a separator, so decoding first would let
    # `remote-agents%2Factivity-plugin.mjs` -- one segment -- read as two.
    parts = [unquote(segment) for segment in PurePosixPath(split.path).parts]
    return Path(*parts)


def _refuse_a_planted_plugin_path(path: Path) -> None:
    """Refuse to generate executable code through a link somebody left standing at our name.

    `_write_atomically` deliberately writes *through* a symlinked settings file, because an
    operator's dotfiles are plausibly symlinked into place and that file is theirs to arrange.
    This file is not theirs, was never anywhere else, and is loaded and run by their agent, so
    the same argument runs the other way: a link here is not a preference, it is a redirection.

    **What is deliberately NOT refused here: a pre-existing ancestor another user could write.**
    That check was written first and had to come out. `~/.config` and `~/.config/opencode` are
    routinely group-writable under the umask most distributions ship (0002 with user-private
    groups), so the refusal fired on an ordinary machine -- including this project's own
    operator's -- and made the provider uninstallable for a group whose only member is the owner.
    It is also not this installer's asymmetry to fix: the two existing providers write hook
    *commands*, which their agents also execute, into `~/.claude` and `~/.codex` with no such
    check. What is left is what actually holds -- `open_private_directory` refuses to create
    through a link at any level of the path and chmods the directory it makes to 0700, and this
    refusal covers the leaf.
    """
    if path.is_symlink():
        raise HookInstallError(
            f"refusing to write the plugin through the symlink at {path}: it points at "
            f"{os.readlink(path)}, which is not where this installer put anything. Remove the "
            "link and run this again."
        )


def _write_plugin(path: Path, source: str, marker: str) -> bool:
    """Put the generated file in place owner-only, and report whether it changed.

    Reported rather than assumed because a reinstall after an upgrade changes this file while
    leaving the config entry -- which names a path, not a version -- byte-identical. An install
    that answered "already current" on the strength of the config alone would leave the old
    plugin running and say nothing. A repaired *mode* counts as changed for the same reason: it
    is something an operator re-running the installer would want to hear about.

    Refuses to overwrite a file that does not carry the marker, which is the same asymmetry
    `_remove_plugin` is built on, applied to the other irreversible act. A Tier-1 review pointed
    out that only half of it was guarded: removal checked before deleting, and install did not
    check before overwriting -- and overwriting somebody's file destroys its content exactly as
    permanently as deleting it. The marker is a stable prefix and must stay one; changing its
    text would make every already-installed host refuse its next install.

    **What that guard does and does not close, stated exactly.** It is a check followed by a
    write, so it is a check-then-act like every other one here. The *symlink* race is genuinely
    closed rather than narrowed, because the write does not follow a link
    (`_write_atomically(follow_symlink=False)`) and so loses harmlessly. The marker check is not:
    a same-user process that swaps a different unmarked regular file in between this check and
    the rename has that file replaced anyway, since `os.replace` does not re-read the marker. A
    Tier-2 review found this paragraph claiming more than that. It is left as a bounded, stated
    exposure rather than closed, on the ground `open_private_directory`'s docstring already
    gives: a same-user process can always win such a race, and the reachable case -- a file left
    lying at that path -- is the one a check can answer.
    """
    content = source.encode("utf-8")
    _refuse_a_planted_plugin_path(path)
    _refuse_a_plugin_file_we_did_not_write(path, marker)
    # Before the content check, not after it, and unconditionally. A gate evaluator measured the
    # other order: an operator (or anything running as them) who chmods this file 0666 kept it,
    # because a reinstall found the bytes equal, answered "already current" and returned before
    # reaching the mode. A world-writable file the agent loads and executes surviving every
    # subsequent install is the exact risk this stage declared, repaired on no run at all.
    tightened = _open_our_directory(path.parent)
    # Whole bytes, never a prefix. Comparing only the first `len(content)` bytes -- which this
    # briefly did, as a fix for reading a whole file to inspect one line -- reads a file holding
    # our exact content *plus appended code* as unchanged, so an install would decline to repair
    # the one shape most worth repairing. The same evaluator caught that in the working tree
    # before it was committed. `_head` is for the marker checks, where a prefix is the question.
    if path.is_file() and path.read_bytes() == content:
        # BOTH modes, not just the file's. The first version of this counted only the file, which
        # left the same defect one line up half-fixed -- and on the more dangerous half: 0777 on
        # the directory lets anyone unlink the 0600 plugin and leave a file of their own, which
        # is the planted-file case this whole chain exists for. The exposure was always closed
        # (the directory is tightened unconditionally); it was the *reporting* that was silent.
        repaired = stat.S_IMODE(path.stat().st_mode) != 0o600
        os.chmod(path, 0o600)
        return repaired or tightened
    _write_atomically(path, content, 0o600, follow_symlink=False)
    return True


def _open_our_directory(directory: Path) -> bool:
    """Create the one directory this installer owns, owner-only, refusing a link at *its* name.

    `open_private_directory` refuses a symlink at **any** component of the path, which is right
    for a spool living under a state directory this project made. It is wrong here, and an
    adversarial review measured why: an operator whose dotfiles are managed by `stow` has
    `~/.config/opencode` symlinked into a repository, and the provider became uninstallable for
    them with a message naming the leaf and never the link -- no path forward from the output.

    A symlinked *ancestor* is the operator's own arrangement, which is exactly the argument
    `_write_atomically` makes for writing through a symlinked settings file. A symlink standing
    at the directory this installer creates is not: nothing put one there but somebody else.
    So the ancestors are followed and only the leaf is refused, and the refusal names the link
    and its target rather than the directory the operator can see is fine.

    Returns whether an existing directory's mode had to be tightened, so an install that repaired
    one can say so instead of answering "already current" over it.
    """
    if directory.is_symlink():
        raise HookInstallError(
            f"refusing to create the plugin directory through the symlink at {directory}: it "
            f"points at {os.readlink(directory)}, which is not where this installer put "
            "anything. Remove the link and run this again."
        )
    if not directory.parent.is_dir():
        raise HookInstallError(
            f"{directory.parent} does not exist, so there is nowhere to put the plugin; "
            "nothing was written"
        )
    try:
        loose = directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) != 0o700
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as error:
        raise HookInstallError(
            f"cannot create {directory} as an owner-only directory ({error}); it has been left "
            "alone and nothing was written"
        ) from error
    return loose


def _head(path: Path, size: int) -> bytes:
    """Read only what a comparison needs, rather than the whole file to inspect its first line."""
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError as error:
        raise HookInstallError(f"cannot read {path}: {error}") from error


def _refuse_a_plugin_file_we_did_not_write(path: Path, marker: str) -> None:
    """Refuse to overwrite anything standing at our name that this project did not generate.

    *Anything*, not just a regular file. The first version returned early on `not path.is_file()`,
    which meant a FIFO, a socket or a device node at that name fell straight past the guard and
    was replaced -- found by an adversarial review, which stood a FIFO there and watched the
    install report success. The guard's stated intent was always "we did not write this", and a
    named pipe somebody is reading from is as much theirs as a file is.
    """
    if not path.exists():
        return
    if not path.is_file():
        raise HookInstallError(
            f"refusing to replace {path}: something is already there and it is not a regular "
            "file, so it was not written by this project. Move it aside and run this again; "
            "nothing else has been touched."
        )
    expected = marker.encode("utf-8")
    if _head(path, len(expected)) == expected:
        return
    raise HookInstallError(
        f"refusing to overwrite {path}, because it does not begin with "
        f"{marker!r} and so was not written by this project. Move it aside and run this "
        "again; nothing else has been touched."
    )


def _remove_plugin(path: Path, provider: _HookProvider) -> bool:
    """Delete the generated file, and only ever a file that says this project generated it."""
    assert provider.plugin is not None
    if path.is_symlink() or not path.is_file():
        return False
    expected = provider.plugin.marker.encode("utf-8")
    if _head(path, len(expected)) != expected:
        return False
    try:
        path.unlink()
    except OSError as error:
        # A sentence, not a traceback. `bootstrap` catches `HookInstallError` and prints it; a
        # bare `PermissionError` reaching there shows the operator a stack trace instead -- and
        # since the config entry is written before this runs, it would abort a run that has
        # already half-succeeded. A round-2 verification pass found it by chmod-ing the
        # directory read-only between the two.
        raise HookInstallError(
            f"the {provider.name} plugin entry has been removed from the configuration, but "
            f"{path} itself could not be deleted ({error}). Nothing loads it now; delete it by "
            "hand, or fix the permission and run this again."
        ) from error
    return True
