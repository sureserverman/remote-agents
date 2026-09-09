"""The one table mapping each curated provider to its declared capabilities (ARCH-04).

`ProfileUsageReaders` proved the shape in miniature — a closed dispatch from provider
identity to the object that answers for it — and this module generalizes it: one
`ProviderDescriptor` per provider, every capability either the wired object or a declared
`None` (DEC-061). This is the only module that imports every provider's adapter code;
composition consumes the table and nothing below it (ARCH-02).

`sessions` is a factory taking the live project-path mapping, because a conversation
catalogue is scoped to the workspaces a host currently offers while the registry itself is
not. `hooks` carries the provider name this module's install surface accepts — the values
themselves live in each vertical's `hooks.py`. `activity` is declaredly `None` for all four:
the codex approval watch is composed at the service boundary (it needs the store and the
terminal), and the hook spool is not per-provider wiring.

Each vertical builds its own descriptor (`<provider>.descriptor()`); this module also
carries the two cross-provider surfaces the retired flat modules held: the usage dispatch
(`ProfileUsageReaders`) and the hook-install API (`install_agent_hooks` and friends, whose
refusal-ladder design record is kept verbatim below as `_HOOK_INSTALL_DESIGN_RECORD`).

The usage seam's design record, moved whole from the retired `usage.py`:

Read what each provider has spent, from the working files the provider itself maintains.

Nothing here asks an agent anything, starts a process, or touches the network. Every number
below is lifted out of a file the provider was going to write regardless, which is what makes
a usage read safe to do from inside a Telegram render: the worst case is a few kilobytes of
tail-reading and an answer of `None`.

**The providers publish very different amounts, and the asymmetry is the whole shape of this
module.** Measured on this host on 2026-08-27 rather than taken from documentation, because
none of these formats is documented and all of them are free to change:

| profile       | context window                      | rate-limit windows              |
| ------------- | ----------------------------------- | ------------------------------- |
| claude        | transcript `message.usage` per turn | none written down (see below)   |
| claude-remote | as claude                           | as claude                       |
| codex         | rollout `token_count.info`          | rollout `token_count`'s limits  |
| opencode      | `opencode.db` `message.data.tokens` | none written down               |
| cursor-agent  | nothing — see `CursorUsageReader`   | nothing                         |

**Claude's limits are the one number that is not the session's own.** Claude Code receives
`rate_limits` from the API and hands them to a *status line* command; it never persists them.
The only durable copy on this host is the cache the owner's own `~/.claude/statusline.sh`
writes to `/tmp/claude/statusline-usage-cache-<hash>.json` after calling the OAuth usage
endpoint. Reading it is a deliberate, owner-approved coupling to a file this project does not
own, and it is fenced accordingly: the figure is stamped `stale_source` so presentation always
says where it came from, an unreadable or absent cache is simply no answer, and a cache older
than `_STALE_LIMIT_AGE` is discarded rather than shown. The alternative — this service holding
the owner's OAuth token and calling the endpoint itself — would have given the bot network
egress and credential access it has never had, for one line on one screen.

**Matching a managed session to a provider conversation.** A resumed session already names its
conversation (`UsageQuery.resume_source_id`) and every reader short-circuits on it. A fresh
launch does not, so the conversation is found by the two facts the service does know: the
workspace the pane was opened in, and when. That is a heuristic, and it is bounded to the one
case it can get wrong — two sessions launched into the *same* directory with the *same* profile
inside the same window, which is the arrangement `Concurrent Agent Sessions Share One Checkout`
already advises against. It cannot silently attribute another *project's* usage to a session,
because the workspace is matched exactly.
"""

from __future__ import annotations

import os
import shlex
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.agents import claude, codex, cursor, opencode
from remote_agents.adapters.agents.claude.hooks import PROVIDER as _CLAUDE
from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.adapters.agents.codex.hooks import PROVIDER as _CODEX
from remote_agents.adapters.agents.codex.usage import CodexUsageReader
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.adapters.agents.hook_settings import (
    HookInstallError,
    _foreign_variant_note,
    _HookProvider,
    _read_settings,
    _refuse_a_planted_plugin_path,
    _refuse_a_spool_others_can_reach,
    _refuse_if_changed_since_it_was_read,
    _refuse_when_removal_would_not_restore,
    _remove_plugin,
    _with_our_groups,
    _without_our_groups,
    _write_atomically,
    _write_plugin,
)
from remote_agents.adapters.agents.opencode.hooks import PROVIDER as _OPENCODE
from remote_agents.adapters.agents.opencode.usage import OpenCodeUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    LimitsAbsence,
    UsageQuery,
)
from remote_agents.ports.provider_descriptor import ProviderDescriptor

ProjectPaths = Mapping[ProjectId, Path]


class ProfileUsageReaders:
    """Dispatch a usage query to the reader for its profile, and never raise at a caller.

    Total by construction: an unknown profile, an unreadable file, a database another program
    has locked, a JSON document whose shape changed under an upgrade — all of them are one
    session's usage line going missing, and none of them is worth failing the screen that line
    sits on. That is the same trade `ClaudeSessionCatalogue` makes for the resume catalogue and
    the same one `activity_spool` makes inside the hook, for the same reason: this is a
    decoration on a screen whose real content is the session's state and its actions.
    """

    def __init__(
        self,
        readers: Iterable[object] | None = None,
        *,
        context_window: int | None = None,
        context_window_stated: bool = False,
    ) -> None:
        resolved = tuple(
            readers
            if readers is not None
            else (
                ClaudeUsageReader(
                    context_window=context_window, context_window_stated=context_window_stated
                ),
                CodexUsageReader(),
                OpenCodeUsageReader(),
                CursorUsageReader(),
            )
        )
        self._readers = resolved
        self._by_profile = {
            profile: reader
            for reader in resolved
            for profile in reader.profiles  # type: ignore[attr-defined]
        }

    @property
    def profiles(self) -> frozenset[ProfileId]:
        """Which profiles this set can answer for, so a gap is assertable rather than latent.

        A curated profile with no reader answers `None` forever, and `None` renders as "no
        conversation matched yet" — a sentence that invites the owner to wait for something that
        is never coming. That is the failure a coverage test needs to be able to see.
        """
        return frozenset(self._by_profile)

    def limits(self) -> tuple[AgentLimits, ...]:
        """One entry per reader, in composition order, and never an exception at a caller.

        Per *reader* rather than per profile: `ClaudeUsageReader` answers for two profiles that
        share one account, and an entry each would render one plan's windows twice under two
        names. `limits_profile` is what each reader files its answer under.

        A reader that fails still contributes its entry, carrying no windows. Dropping it
        instead would be indistinguishable, on the screen, from a provider that publishes
        nothing — and those two are exactly the cases DEC-061 requires stay apart.
        """
        answers = []
        for reader in self._readers:
            # Read inside the guard, not before it. `__init__` takes `Iterable[object]` with no
            # protocol, so a reader without a label is reachable -- and reading it outside the
            # try raised `AttributeError` straight through the boundary this docstring promises
            # never raises. An unlabelled reader is skipped rather than given a fallback name,
            # because there is no honest name to give it.
            try:
                profile = reader.limits_profile  # type: ignore[attr-defined]
            except AttributeError:
                # Narrowed to the attribute access alone. Wrapping the `limits()` call in the
                # same guard swallowed an `AttributeError` raised *inside* a reader — a real
                # bug — and dropped its entry, which is the opposite of what the next clause
                # promises. `__init__` takes `Iterable[object]` with no protocol, so an
                # unlabelled reader is reachable; it is skipped rather than given a name it
                # does not have.
                continue
            try:
                answers.append(reader.limits())  # type: ignore[attr-defined]
            except (OSError, ValueError, ArithmeticError, sqlite3.Error):
                # `UNREADABLE`, not a bare empty answer. This branch is the one absence that
                # names a *fault* -- the provider's files are there and this process could not
                # read them -- and rendering it the way a provider that publishes nothing
                # renders would hide a broken host behind a legitimate silence (DEC-061).
                answers.append(AgentLimits(profile, absence=LimitsAbsence.UNREADABLE))
        return tuple(answers)

    def read(self, query: UsageQuery) -> AgentUsage | None:
        reader = self._by_profile.get(query.profile_id)
        if reader is None:
            return None
        try:
            return reader.read(query)  # type: ignore[attr-defined]
        except (OSError, ValueError, ArithmeticError, sqlite3.Error):
            return None


def provider_descriptors(
    *,
    claude_context_window: int | None = None,
    claude_context_window_stated: bool = False,
) -> tuple[ProviderDescriptor, ...]:
    """One descriptor per provider, in stable UI order, each built by its own vertical.

    The two keyword arguments thread the one owner-configurable capability through to
    claude's builder (DEC-061 — the ceiling reaches the reader only when the owner stated
    it).
    """
    return (
        claude.descriptor(
            context_window=claude_context_window,
            context_window_stated=claude_context_window_stated,
        ),
        codex.descriptor(),
        opencode.descriptor(),
        cursor.descriptor(),
    )


def glyph_of(profile_id: ProfileId) -> str:
    """The mark a surface draws for one *profile*, or nothing at all.

    Profiles and providers are not the same set: five curated spellings, four verticals.
    `claude-remote` is `claude --remote-control` — the same binary under a second curated
    name (`domain/profiles.py`) — so it must draw claude's mark, and this is the same
    resolution `ProfileUsageReaders` performs when it files both spellings under one reader.

    **Resolved through the executable the domain already curates, not through an alias table
    kept here.** A table would work today and would be a registry edit every time a vertical
    gained a second spelling, which is precisely the edit DEC-070 says a new provider must
    not cost: a fifth provider declares its mark in its own package, and any `-remote`-style
    spelling of it curated in `domain/profiles.py` resolves here for free. The registry adds
    no mark of its own; it only answers with one a vertical declared.

    Total, and empty for anything unrecognised — the trade `ProfileUsageReaders.read` makes,
    for the same reason: the caller is a render, and a render that raises over one unknown
    profile takes the whole keyboard with it. Empty is also what both surfaces collapse to
    the label they drew before this field existed.
    """
    by_provider = {
        str(descriptor.profile_id): descriptor.glyph for descriptor in provider_descriptors()
    }
    name = str(profile_id)
    if name in by_provider:
        return by_provider[name]
    executables = {str(profile.profile_id): profile.executable for profile in closed_profiles()}
    return by_provider.get(executables.get(name, ""), "")


def profile_glyphs() -> dict[str, str]:
    """Every curated profile's mark, as one mapping a composition can hand to a surface.

    The fold `usage_readers` is for usage, done for identity: a frontend gets the whole
    answer once, at composition time, rather than reaching into the registry from a render.
    Keyed by the profile's string spelling because that is what a session record carries.

    It exists as a named function rather than a comprehension at the composition site so
    that what a surface is handed and what a test asserts about are the same expression —
    a second copy of the fold would agree with this one on the day it was written and
    answer to nothing afterwards. It folds over the *profiles*, not the descriptors: there
    are five of the first and four of the second, and the fold that forgets that is one
    where `claude-remote` silently loses its mark.
    """
    return {str(profile.profile_id): glyph_of(profile.profile_id) for profile in closed_profiles()}


def usage_readers(descriptors: tuple[ProviderDescriptor, ...]) -> ProfileUsageReaders:
    """Fold the registry's usage capabilities into the one dispatch both surfaces share.

    Built from the descriptors rather than from this module's own list, so the registry —
    not a second table — decides which providers answer usage queries (DEC-046: one set of
    readers per host).
    """
    return ProfileUsageReaders(
        readers=tuple(
            descriptor.usage for descriptor in descriptors if descriptor.usage is not None
        )
    )


# --- The hook-install surface -----------------------------------------------------------
#
# Moved whole from the retired `hook_install.py`; its module docstring below is the design
# record for why every refusal exists, kept verbatim. The constant is deliberately inert
# prose — source-readable documentation, not dead code; do not "clean it up".

_HOOK_INSTALL_DESIGN_RECORD = """\
Merge this project's agent-event hooks into a Claude Code settings file, reversibly.

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


_PROVIDERS = {provider.name: provider for provider in (_CLAUDE, _CODEX, _OPENCODE)}


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


def default_settings_path(
    home: Path, *, provider: str = "claude", environment: Mapping[str, str] | None = None
) -> Path:
    """Locate the settings file the agent reads, given the home directory to look under.

    **`XDG_CONFIG_HOME` is honoured for a provider whose config lives under `.config`**, which
    today is opencode alone -- claude and codex keep their own dotdirectories and are untouched
    by this branch. A gate evaluator found the assumption undisclosed and pointed at this
    stage's own evidence for why it matters: `docs/acceptance-2026-09-06-opencode-activity.md`
    relocated OpenCode's configuration with exactly that variable to run the measurement. On a
    host that sets it, assuming `~/.config` would create a config file and a plugin that OpenCode
    never reads, and report `installed the opencode activity plugin in ...` over the top.

    A *relative* value is ignored rather than resolved, as the XDG specification requires: it
    would resolve against whichever directory this command happened to be run from, which is the
    same failure `_refuse_a_spool_others_can_reach` refuses a relative `--activity-dir` for.
    """
    selected = _provider(provider)
    relative = selected.configuration_relative_path
    if relative.parts and relative.parts[0] == ".config":
        # Resolved here rather than bound as a default at import: `os.environ` as a default
        # argument is a live mapping and so reads correctly for `monkeypatch.setenv`, but it
        # freezes the *object*, which a test replacing `os.environ` wholesale would not see.
        # Passing `{}` is also how a caller says "answer about this home, not this shell" --
        # which two tests needed and did not have.
        resolved = os.environ if environment is None else environment
        configured = resolved.get("XDG_CONFIG_HOME")
        if configured and Path(configured).is_absolute():
            return Path(configured).joinpath(*relative.parts[1:])
    return home / relative


def _agent_event_argv(
    executable: Path, activity_directory: Path | None, provider: _HookProvider
) -> list[str]:
    """Spell the spool command as argv, which is the form that cannot be re-split.

    The one source both shapes are rendered from. A hook settings file wants a string, and the
    generated plugin spawns without a shell and wants a list; deriving the string from the list
    means the two spellings cannot drift into disagreeing about which command this project
    installed.
    """
    argv = [str(executable), "-m", "remote_agents", "agent-event"]
    if not provider.flagless:
        argv += ["--provider", provider.name]
    if activity_directory is not None:
        argv += ["--activity-dir", str(activity_directory)]
    return argv


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
    return shlex.join(_agent_event_argv(executable, activity_directory, _provider(provider)))


def _plugin_path(settings_path: Path, provider: _HookProvider) -> Path:
    """Where this provider's generated file lands, derived from the settings file's own home.

    Derived rather than resolved against `Path.home()`, so `--settings` moves both artifacts
    together: the live drill and every test here point at a temporary tree, and a plugin that
    kept landing in the operator's real configuration while its entry went somewhere else would
    be the worst of both.
    """
    assert provider.plugin is not None
    return settings_path.parent / provider.plugin.relative_path


def install_agent_hooks(
    settings_path: Path,
    *,
    executable: Path | None = None,
    activity_directory: Path | None = None,
    provider: str = "claude",
) -> HookInstallOutcome:
    """Add one group per event — or one plugin entry — replacing any left behind previously."""
    _refuse_a_spool_others_can_reach(activity_directory)
    selected = _provider(provider)
    if selected.plugin is not None:
        # Before the settings file is even read: this refusal is about where generated code
        # would land, and an operator with a link standing at that name should hear about the
        # link rather than about their JSON.
        _refuse_a_planted_plugin_path(_plugin_path(settings_path, selected))
    settings = _read_settings(settings_path, selected)
    interpreter = Path(sys.executable) if executable is None else executable
    argv = _agent_event_argv(interpreter, activity_directory, selected)
    our_plugin = _plugin_path(settings_path, selected) if selected.plugin is not None else None
    ours = our_plugin.as_uri() if our_plugin is not None else shlex.join(argv)
    base = _without_our_groups(settings.document, selected, our_plugin)
    installed = _with_our_groups(base, ours, selected)
    _refuse_when_removal_would_not_restore(settings, base, installed, selected, our_plugin)
    content = settings.style.render(installed)
    # Reported on both paths. Re-running the installer is exactly what an operator does when
    # they are trying to work out why every event arrives twice, and answering "already
    # current" while saying nothing about the variant that is doubling them is the least
    # helpful moment to stay quiet.
    note = _foreign_variant_note(base, selected, our_plugin)
    if content == settings.content:
        # The config entry names a path and never a version, so it is byte-identical across an
        # upgrade that rewrote the plugin. Whether anything changed is therefore the plugin's
        # answer here, not the config's.
        refreshed = selected.plugin is not None and _write_plugin(
            _plugin_path(settings_path, selected),
            selected.plugin.render(argv),
            selected.plugin.marker,
        )
        if not refreshed:
            return HookInstallOutcome(
                settings_path, False, f"agent hooks already current in {settings_path}{note}"
            )
        return HookInstallOutcome(
            settings_path,
            True,
            f"refreshed the {selected.name} activity plugin beside {settings_path}{note}",
        )
    _refuse_if_changed_since_it_was_read(settings_path, settings.content)
    if selected.plugin is not None:
        # The file before the entry that names it: an entry pointing at a file that is not there
        # yet is a load error in the operator's next session, while a file nothing points at is
        # inert. Ordered for the failure that costs less -- and safe to order that way only
        # because `_remove_plugin` deletes by marker without needing a config entry to exist, so
        # an interrupted install between these two writes is recoverable by `--remove` alone.
        # That invariant is load-bearing for this ordering; an edit to `_remove_plugin` that made
        # deletion conditional on the entry would strand the file this line writes.
        _write_plugin(
            _plugin_path(settings_path, selected),
            selected.plugin.render(argv),
            selected.plugin.marker,
        )
        summary = f"installed the {selected.name} activity plugin in {settings_path}"
    else:
        summary = (
            f"installed {len(selected.installed_events)} {selected.name} agent hooks "
            f"in {settings_path}"
        )
    _write_atomically(settings_path, content, settings.mode)
    return HookInstallOutcome(settings_path, True, summary + note)


def remove_agent_hooks(settings_path: Path, *, provider: str = "claude") -> HookInstallOutcome:
    """Delete only this installer's own groups, leaving anything sharing an event alone.

    **Two artifacts, and the second one is collected whatever the first says.** Every refusal and
    early return below was written when the settings file was the only thing an install produced,
    so each of them aborted before the generated plugin was even considered — and a close-out
    evaluator proved what that costs on the ordinary path: install writes the plugin file *first*,
    so a config write that fails on a fresh host leaves executable code in the operator's
    configuration, and `--remove` then answered `no settings file at <path>` and exited 0 over the
    top of it. A missing config, an unparseable one and a concurrently-changed one all did the
    same. DEC-076 names "removal deletes by marker without requiring the entry to exist" as the
    invariant that makes install's ordering safe; it is implemented here rather than assumed.
    """
    selected = _provider(provider)
    our_plugin = _plugin_path(settings_path, selected) if selected.plugin is not None else None
    if not settings_path.exists():
        # Not an error: uninstalling from a machine that was never installed to, and from one
        # whose settings file has since been deleted, should look the same and cost nothing --
        # except that the second of those two may still have our plugin file sitting beside it.
        if our_plugin is not None and _remove_plugin(our_plugin, selected):
            return HookInstallOutcome(
                settings_path,
                True,
                f"removed the {selected.name} activity plugin beside {settings_path}, which had "
                "no settings file left to name it",
            )
        return HookInstallOutcome(settings_path, False, f"no settings file at {settings_path}")
    try:
        settings = _read_settings(settings_path, selected)
    except HookInstallError as error:
        # NOT collected here, and the difference from the branch above is the whole rule: a
        # missing config can never name our file again, while an unreadable one still holds the
        # entry and the operator is going to repair it. Deleting the plugin now would hand them a
        # repaired config naming a file that is gone. So the refusal grows a sentence instead.
        if our_plugin is not None and our_plugin.is_file():
            raise HookInstallError(
                f"{error} The {selected.name} activity plugin at {our_plugin} was therefore left "
                "in place too; repair the file and run this again, or delete both by hand."
            ) from error
        raise
    content = settings.style.render(_without_our_groups(settings.document, selected, our_plugin))
    if content != settings.content:
        # The config write is deliberately NOT wrapped to collect the plugin on failure. The
        # entry survives a failed write, so deleting the file it names would strand it -- which
        # is exactly the defect the ordering below was reversed to fix, reintroduced one branch
        # over. `test_a_removal_that_cannot_write_the_config_has_not_yet_deleted_the_plugin`
        # caught a first draft of this repair doing precisely that.
        _refuse_if_changed_since_it_was_read(settings_path, settings.content)
        _write_atomically(settings_path, content, settings.mode)
    # The entry first, the file second -- the mirror of install's order, and safe for the same
    # reason. An adversarial review found this the other way round and demonstrated the cost: a
    # removal that deleted the file and then failed to write the config left an entry naming a
    # file that is gone, which is a load error in the operator's next session, while
    # `_refuse_if_changed_since_it_was_read` was still telling them "Nothing was lost". Reversed,
    # a failure between the two leaves an orphaned file that nothing points at -- inert, and
    # collected by a re-run, because `_remove_plugin` deletes by marker and needs no entry.
    #
    # Attempted whatever the config said. The two artifacts can legitimately disagree -- a
    # hand-removed entry leaving the file, an interrupted install leaving the file before the
    # entry -- and a removal that only ran when the entry was present would leave executable
    # code in the operator's configuration with nothing left that knows how to take it out.
    deleted = our_plugin is not None and _remove_plugin(our_plugin, selected)
    if content != settings.content:
        return HookInstallOutcome(settings_path, True, f"removed agent hooks from {settings_path}")
    if not deleted:
        return HookInstallOutcome(settings_path, False, f"no agent hooks in {settings_path}")
    return HookInstallOutcome(
        settings_path,
        True,
        f"removed the {selected.name} activity plugin beside {settings_path}",
    )
