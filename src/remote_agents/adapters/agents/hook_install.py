"""Merge this project's agent-event hooks into a Claude Code settings file, reversibly.

The file this writes is not ours. It is the operator's live agent configuration, it holds
their own hooks — including, on the machine this was written for, a ``SessionEnd`` hook of
their own, under an event this installer used to write to and no longer does (DEC-051) — and a
bad write to it breaks every agent session on the machine at once. So the whole module is
arranged around leaving that file exactly as it was found apart from the groups it adds, and
around refusing outright when it cannot promise that.

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
``SessionEnd`` each discriminate on a field of their own — ``error``, ``notification_type``,
``reason``, as the installed bundle spells them — and a matcher here would have to enumerate
the values each can take. The spool on the other end already reads whichever of those fields
an event happens to carry, so filtering in the settings file would only add a second place to
keep in step with upstream, and a value added there would go silently unreported. Every
instance is received and the application layer decides. Two of those three names were wrong
here until the Stage 3 gate compared them with the agent rather than with our own fixtures;
see ``activity_spool._DISCRIMINATING_FIELDS`` for what that cost.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from remote_agents.adapters.agents.hook_settings import (
    HookInstallError,
    _HookProvider,
    _read_settings,
    _refuse_if_changed_since_it_was_read,
    _Settings,
    _write_atomically,
)
from remote_agents.ports.private_directory import ancestors_writable_by_others

INSTALLED_EVENTS = ("Stop", "StopFailure", "Notification")

RETIRED_EVENTS = ("SessionEnd",)
"""Events this installer used to own and must still clean up after.

**Without this, dropping an event strands it for ever.** `_without_our_groups` opens by
skipping any event it does not own, so an event removed from `INSTALLED_EVENTS` stops being
inspected at all -- and our group under it is copied across untouched by *both* the install
path and the uninstall path, which share the predicate. The hook would go on firing on every
host with none of this project's tooling able to remove it, and it could not be worked around
by uninstalling first, because that would mean running the *old* uninstall before taking the
upgrade.

So the sweep is over what we own **now or ever did**, and an event leaves `INSTALLED_EVENTS`
by moving here rather than by disappearing. An entry stays until every host has run the
installer at least once since the event was dropped; there is no way for this process to know
when that is, and the cost of keeping one is a dictionary lookup per install.

`SessionEnd` was dropped 2026-08-23 (DEC-051): its record was spooled, read, deleted and then
discarded at the mapping, because there is no `ActivityKind` for it -- `ended` was retired for
reporting an exit the owner had just caused. It wrote a file per session end, in every Claude
session on the machine, that nothing ever consumed.
"""

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


_CLAUDE = _HookProvider("claude", Path(".claude/settings.json"), INSTALLED_EVENTS, RETIRED_EVENTS)
_CODEX = _HookProvider("codex", Path(".codex/hooks.json"), ("Stop", "PermissionRequest"))
_PROVIDERS = {provider.name: provider for provider in (_CLAUDE, _CODEX)}


def _provider(name: str) -> _HookProvider:
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise HookInstallError(f"unsupported hook provider: {name}") from None


@dataclass(frozen=True, slots=True)
class HookInstallOutcome:
    """What one install or removal actually did, for a caller that has to report it."""

    settings_path: Path
    changed: bool
    summary: str


def default_settings_path(home: Path, *, provider: str = "claude") -> Path:
    """Locate the settings file the agent reads, given the home directory to look under."""
    return home / _provider(provider).configuration_relative_path


def agent_event_command(
    executable: Path, activity_directory: Path | None = None, *, provider: str = "claude"
) -> str:
    """Spell the hook command against a named interpreter rather than the caller's PATH.

    A hook runs with whatever environment the agent happened to have, and the console script
    lives in a virtualenv's ``bin`` that need not be on it — a hook that silently fails to
    resolve is worse than no hook, because nothing reports it. Naming the interpreter that is
    performing the install fixes the resolution at a moment when it is known to be correct:
    that interpreter is by definition one that can import this package.
    """
    selected = _provider(provider)
    command = f"{shlex.quote(str(executable))} -m remote_agents agent-event"
    if selected is not _CLAUDE:
        command = f"{command} --provider {selected.name}"
    if activity_directory is None:
        return command
    return f"{command} --activity-dir {shlex.quote(str(activity_directory))}"


def install_agent_hooks(
    settings_path: Path,
    *,
    executable: Path | None = None,
    activity_directory: Path | None = None,
    provider: str = "claude",
) -> HookInstallOutcome:
    """Add one group per event, replacing any this installer left behind previously."""
    _refuse_a_spool_others_can_reach(activity_directory)
    selected = _provider(provider)
    settings = _read_settings(settings_path, selected)
    interpreter = Path(sys.executable) if executable is None else executable
    base = _without_our_groups(settings.document, selected)
    installed = _with_our_groups(
        base, agent_event_command(interpreter, activity_directory, provider=provider), selected
    )
    _refuse_when_removal_would_not_restore(settings, base, installed, selected)
    content = settings.style.render(installed)
    # Reported on both paths. Re-running the installer is exactly what an operator does when
    # they are trying to work out why every event arrives twice, and answering "already
    # current" while saying nothing about the variant that is doubling them is the least
    # helpful moment to stay quiet.
    note = _foreign_variant_note(base, selected)
    if content == settings.content:
        return HookInstallOutcome(
            settings_path, False, f"agent hooks already current in {settings_path}{note}"
        )
    _refuse_if_changed_since_it_was_read(settings_path, settings.content)
    _write_atomically(settings_path, content, settings.mode)
    summary = (
        f"installed {len(selected.installed_events)} {selected.name} agent hooks in {settings_path}"
    )
    return HookInstallOutcome(settings_path, True, summary + note)


def remove_agent_hooks(settings_path: Path, *, provider: str = "claude") -> HookInstallOutcome:
    """Delete only this installer's own groups, leaving anything sharing an event alone."""
    if not settings_path.exists():
        # Not an error: uninstalling from a machine that was never installed to, and from one
        # whose settings file has since been deleted, should look the same and cost nothing.
        return HookInstallOutcome(settings_path, False, f"no settings file at {settings_path}")
    selected = _provider(provider)
    settings = _read_settings(settings_path, selected)
    content = settings.style.render(_without_our_groups(settings.document, selected))
    if content == settings.content:
        return HookInstallOutcome(settings_path, False, f"no agent hooks in {settings_path}")
    _refuse_if_changed_since_it_was_read(settings_path, settings.content)
    _write_atomically(settings_path, content, settings.mode)
    return HookInstallOutcome(settings_path, True, f"removed agent hooks from {settings_path}")


def _with_our_groups(
    document: dict[str, Any], command: str, provider: _HookProvider = _CLAUDE
) -> dict[str, Any]:
    """Append one matcherless group per event, without disturbing any key's position."""
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
    document: dict[str, Any], provider: _HookProvider = _CLAUDE
) -> dict[str, Any]:
    """Drop our groups, and the containers left holding nothing once they are gone.

    Every other event, and every other group under the events we do install into, is copied
    across untouched — including a group of somebody else's that happens to share
    ``SessionEnd`` with ours, which is the case the operator's real file presents.
    """
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


def _foreign_variant_note(base: dict[str, Any], provider: _HookProvider = _CLAUDE) -> str:
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


def _mentions_our_subcommand(group: Any, provider: _HookProvider = _CLAUDE) -> bool:
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
        prefix = [] if provider is _CLAUDE else [_PROVIDER_OPTION, provider.name]
        if tail[: len(prefix)] == prefix:
            return True
    return False


def _is_our_group(group: Any, provider: _HookProvider = _CLAUDE) -> bool:
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


def _runs_our_command(command: str, provider: _HookProvider = _CLAUDE) -> bool:
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
    prefix = [] if provider is _CLAUDE else [_PROVIDER_OPTION, provider.name]
    if rest[: len(prefix)] != prefix:
        return False
    remaining = rest[len(prefix) :]
    return not remaining or (len(remaining) == 2 and remaining[0] == _ACTIVITY_DIRECTORY_OPTION)


def _refuse_when_removal_would_not_restore(
    settings: _Settings,
    base: dict[str, Any],
    installed: dict[str, Any],
    provider: _HookProvider = _CLAUDE,
) -> None:
    """Run the removal now and refuse the install unless it lands back on the original bytes.

    Two things can go wrong, and this catches both before anything is written. Removal might
    not undo the install — and removal might not be faithful to what is already on disk. The
    second is the reachable one: an empty ``"hooks": {}`` block is indistinguishable, once
    installed into, from a file that never had the key, so removal cannot know whether to
    leave it or delete it. Rather than pick and be wrong half the time, the install is
    refused; deleting the empty block by hand makes it succeed and changes nothing else.
    """
    restored = settings.style.render(_without_our_groups(installed, provider))
    # On a reinstall the bytes on disk already hold our previous groups, so they are not what
    # removal must land on; the check that they were is the one the first install passed.
    faithful = (
        settings.content is None
        or _holds_our_groups(settings.document, provider)
        or settings.style.render(base) == settings.content
    )
    if restored != settings.style.render(base) or not faithful:
        raise HookInstallError(
            f"{settings.path} has been left untouched, because removing these hooks again "
            "could not put it back exactly as it is now. An empty block is almost always the "
            'cause: "hooks": {} and no "hooks" key at all mean the same thing but are '
            "different text, so an uninstall cannot tell which one to leave behind. Delete "
            "the empty block (or the empty list, or a null, under any of "
            f"{', '.join((*INSTALLED_EVENTS, *RETIRED_EVENTS))}) and run this again — that "
            "changes nothing else about your settings."
        )


def _holds_our_groups(document: dict[str, Any], provider: _HookProvider = _CLAUDE) -> bool:
    """Report whether a previous install is present, which is what makes this a reinstall."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        _is_our_group(group, provider)
        for event in provider.installed_events
        for group in hooks.get(event) or ()
    )
