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

from remote_agents.adapters.agents.hook_settings import (
    HookInstallError,
    _foreign_variant_note,
    _HookProvider,
    _read_settings,
    _refuse_a_spool_others_can_reach,
    _refuse_if_changed_since_it_was_read,
    _refuse_when_removal_would_not_restore,
    _with_our_groups,
    _without_our_groups,
    _write_atomically,
)

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


_CLAUDE = _HookProvider(
    "claude", Path(".claude/settings.json"), INSTALLED_EVENTS, RETIRED_EVENTS, flagless=True
)
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
    if not selected.flagless:
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
