"""Merge this project's agent-event hooks into a Claude Code settings file, reversibly.

The file this writes is not ours. It is the operator's live agent configuration, it holds
their own hooks — including, on the machine this was written for, a ``SessionEnd`` hook that
is one of the four events installed here — and a bad write to it breaks every agent session
on the machine at once. So the whole module is arranged around leaving that file exactly as
it was found apart from the four groups it adds, and around refusing outright when it cannot
promise that.

Three decisions carry most of that weight.

*Byte-for-byte removal is proved, not hoped for.* Rewriting a parsed document with
``json.dumps`` picks an indentation, a separator style and a trailing newline of its own, and
whichever it picks is probably not the file's, so removal would hand back a reformatted file
rather than the original. Instead the file's own formatting is *recovered*: candidate styles
are rendered against the untouched parsed document until one reproduces the original bytes
exactly, and that style is what both install and removal serialize with. A file no candidate
reproduces — one holding a number whose text does not survive a float round trip, say — is
refused rather than silently reformatted. On top of that, install runs its own removal against
the document it is about to write and checks the result renders back to the bytes on disk. The
reversibility guarantee is therefore checked on the operator's real file at install time, not
argued for here.

*Our groups are found by what they run — not by what they say.* There is nowhere in the
documented hook schema to put a marker key, and inventing one risks tripping a validator
upstream, so a group is recognised as ours when every command in it, split into words, has
``-m remote_agents agent-event`` immediately after the interpreter, followed by nothing or by
the one option this installer knows how to add — and when the group carries no key but
``hooks``, since the groups written here are matcherless and carry nothing else. Ignoring the
interpreter in front is what makes reinstalling after the virtualenv moves replace the stale
entry instead of adding a second.

Comparing parsed words rather than searching the text is the part that matters. Substring
matching, which is what this did first, made an operator's own hook ours as soon as its
command happened to contain that phrase — a reminder echoed in a wrapper script, a grep in
an auditing one — and ``--remove`` then deleted it outright. Mentioning a command and
running it have to be different things here, because the cost of confusing them is somebody
else's hook, and this module has no way to give that back.

*The hooks are installed without matchers.* ``StopFailure``, ``Notification`` and
``SessionEnd`` each discriminate on a field of their own — ``error_type``,
``notification_type``, ``end_reason`` — and a matcher here would have to enumerate the values
each can take. The spool on the other end already reads whichever of those fields an event
happens to carry, so filtering in the settings file would only add a second place to keep in
step with upstream, and a value added there would go silently unreported. Every instance is
received and the application layer decides.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INSTALLED_EVENTS = ("Stop", "StopFailure", "Notification", "SessionEnd")

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


class HookInstallError(Exception):
    """A settings file this installer will not write to, and the reason why."""


@dataclass(frozen=True, slots=True)
class HookInstallOutcome:
    """What one install or removal actually did, for a caller that has to report it."""

    settings_path: Path
    changed: bool
    summary: str


def default_settings_path(home: Path) -> Path:
    """Locate the settings file the agent reads, given the home directory to look under."""
    return home / ".claude" / "settings.json"


def agent_event_command(executable: Path, activity_directory: Path | None = None) -> str:
    """Spell the hook command against a named interpreter rather than the caller's PATH.

    A hook runs with whatever environment the agent happened to have, and the console script
    lives in a virtualenv's ``bin`` that need not be on it — a hook that silently fails to
    resolve is worse than no hook, because nothing reports it. Naming the interpreter that is
    performing the install fixes the resolution at a moment when it is known to be correct:
    that interpreter is by definition one that can import this package.
    """
    command = f"{shlex.quote(str(executable))} -m remote_agents agent-event"
    if activity_directory is None:
        return command
    return f"{command} --activity-dir {shlex.quote(str(activity_directory))}"


def install_agent_hooks(
    settings_path: Path,
    *,
    executable: Path | None = None,
    activity_directory: Path | None = None,
) -> HookInstallOutcome:
    """Add one group per event, replacing any this installer left behind previously."""
    settings = _read_settings(settings_path)
    interpreter = Path(sys.executable) if executable is None else executable
    base = _without_our_groups(settings.document)
    installed = _with_our_groups(base, agent_event_command(interpreter, activity_directory))
    _refuse_when_removal_would_not_restore(settings, base, installed)
    content = settings.style.render(installed)
    if content == settings.content:
        return HookInstallOutcome(
            settings_path, False, f"agent hooks already current in {settings_path}"
        )
    _refuse_if_changed_since_it_was_read(settings_path, settings.content)
    _write_atomically(settings_path, content, settings.mode)
    summary = f"installed {len(INSTALLED_EVENTS)} agent hooks in {settings_path}"
    return HookInstallOutcome(settings_path, True, summary + _foreign_variant_note(base))


def remove_agent_hooks(settings_path: Path) -> HookInstallOutcome:
    """Delete only this installer's own groups, leaving anything sharing an event alone."""
    if not settings_path.exists():
        # Not an error: uninstalling from a machine that was never installed to, and from one
        # whose settings file has since been deleted, should look the same and cost nothing.
        return HookInstallOutcome(settings_path, False, f"no settings file at {settings_path}")
    settings = _read_settings(settings_path)
    content = settings.style.render(_without_our_groups(settings.document))
    if content == settings.content:
        return HookInstallOutcome(settings_path, False, f"no agent hooks in {settings_path}")
    _refuse_if_changed_since_it_was_read(settings_path, settings.content)
    _write_atomically(settings_path, content, settings.mode)
    return HookInstallOutcome(settings_path, True, f"removed agent hooks from {settings_path}")


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


def _read_settings(path: Path) -> _Settings:
    """Parse and validate a settings file, refusing every shape that cannot be merged into."""
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise HookInstallError(
                f"{path.parent} does not exist, so this machine has no agent configuration to "
                "install into; refusing to create one"
            ) from None
        # A settings file is the agent's, and creating one holding only our four hooks is both
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
    _refuse_unmergeable_hooks(path, document)
    return _Settings(
        path,
        content,
        document,
        _detected_style(path, document, content),
        stat.S_IMODE(path.stat().st_mode),
    )


def _refuse_unmergeable_hooks(path: Path, document: dict[str, Any]) -> None:
    """Reject a hooks block whose shape this installer would have to guess at."""
    hooks = document.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise HookInstallError(
            f'the "hooks" key in {path} is not a JSON object; it has been left untouched'
        )
    for event in INSTALLED_EVENTS:
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


def _with_our_groups(document: dict[str, Any], command: str) -> dict[str, Any]:
    """Append one matcherless group per event, without disturbing any key's position."""
    hooks = dict(document.get("hooks") or {})
    for event in INSTALLED_EVENTS:
        group = {"hooks": [{"type": "command", "command": command}]}
        # `or ()` rather than a `.get` default, because an explicit JSON null defeats the
        # default and unpacking it raised out through the CLI as a traceback. Reading null as
        # "no groups" is what the validator above already decided; the round-trip check then
        # refuses the install anyway, since dropping our groups again would leave the key
        # gone rather than null -- which is the same refusal an empty list gets, and the same
        # message tells the operator to delete it. Same form as `_holds_our_groups`.
        hooks[event] = [*(hooks.get(event) or ()), group]
    return {**document, "hooks": hooks}


def _without_our_groups(document: dict[str, Any]) -> dict[str, Any]:
    """Drop our groups, and the containers left holding nothing once they are gone.

    Every other event, and every other group under the events we do install into, is copied
    across untouched — including a group of somebody else's that happens to share
    ``SessionEnd`` with ours, which is the case the operator's real file presents.
    """
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return document
    remaining: dict[str, Any] = {}
    for event, groups in hooks.items():
        if event not in INSTALLED_EVENTS or not isinstance(groups, list):
            remaining[event] = groups
            continue
        kept = [group for group in groups if not _is_our_group(group)]
        if kept:
            remaining[event] = kept
    if remaining:
        return {**document, "hooks": remaining}
    return {key: value for key, value in document.items() if key != "hooks"}


def _foreign_variant_note(base: dict[str, Any]) -> str:
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
    hooks = base.get("hooks")
    if not isinstance(hooks, dict):
        return ""
    events = [
        event
        for event in INSTALLED_EVENTS
        if any(_mentions_our_subcommand(group) for group in hooks.get(event) or ())
    ]
    if not events:
        return ""
    return (
        f". Note: {', '.join(events)} already runs this subcommand in a form this installer "
        "does not recognise and will not touch, so the hook now runs twice for those events; "
        "removing these hooks later will leave that entry in place"
    )


def _mentions_our_subcommand(group: Any) -> bool:
    """Report a group running our subcommand that `_is_our_group` will not claim."""
    if _is_our_group(group) or not isinstance(group, dict):
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
        if tuple(words[1 : 1 + len(_COMMAND_TAIL)]) == _COMMAND_TAIL:
            return True
    return False


def _is_our_group(group: Any) -> bool:
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
        and _runs_our_command(entry["command"])
        for entry in entries
    )


def _runs_our_command(command: str) -> bool:
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
    return not rest or (len(rest) == 2 and rest[0] == _ACTIVITY_DIRECTORY_OPTION)


def _refuse_when_removal_would_not_restore(
    settings: _Settings, base: dict[str, Any], installed: dict[str, Any]
) -> None:
    """Run the removal now and refuse the install unless it lands back on the original bytes.

    Two things can go wrong, and this catches both before anything is written. Removal might
    not undo the install — and removal might not be faithful to what is already on disk. The
    second is the reachable one: an empty ``"hooks": {}`` block is indistinguishable, once
    installed into, from a file that never had the key, so removal cannot know whether to
    leave it or delete it. Rather than pick and be wrong half the time, the install is
    refused; deleting the empty block by hand makes it succeed and changes nothing else.
    """
    restored = settings.style.render(_without_our_groups(installed))
    # On a reinstall the bytes on disk already hold our previous groups, so they are not what
    # removal must land on; the check that they were is the one the first install passed.
    faithful = (
        settings.content is None
        or _holds_our_groups(settings.document)
        or settings.style.render(base) == settings.content
    )
    if restored != settings.style.render(base) or not faithful:
        raise HookInstallError(
            f"{settings.path} has been left untouched, because removing these hooks again "
            "could not put it back exactly as it is now. An empty block is almost always the "
            'cause: "hooks": {} and no "hooks" key at all mean the same thing but are '
            "different text, so an uninstall cannot tell which one to leave behind. Delete "
            "the empty block (or the empty list, or a null, under Stop, StopFailure, "
            "Notification or SessionEnd) and run this again — that changes nothing else "
            "about your settings."
        )


def _holds_our_groups(document: dict[str, Any]) -> bool:
    """Report whether a previous install is present, which is what makes this a reinstall."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        _is_our_group(group) for event in INSTALLED_EVENTS for group in hooks.get(event) or ()
    )


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


def _write_atomically(path: Path, content: bytes, mode: int) -> None:
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
    """
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
