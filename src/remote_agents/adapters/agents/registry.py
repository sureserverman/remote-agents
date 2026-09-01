"""The one table mapping each curated provider to its declared capabilities (ARCH-04).

`ProfileUsageReaders` proved the shape in miniature — a closed dispatch from provider
identity to the object that answers for it — and this module generalizes it: one
`ProviderDescriptor` per provider, every capability either the wired object or a declared
`None` (DEC-061). This is the only module that imports every provider's adapter code;
composition consumes the table and nothing below it (ARCH-02).

`sessions` is a factory taking the live project-path mapping, because a conversation
catalogue is scoped to the workspaces a host currently offers while the registry itself is
not. `hooks` carries the provider name `hook_install` accepts — the per-provider hook
configuration itself stays inside `hook_install` until the provider verticals land.
`activity` is declaredly `None` for all four today: the codex approval watch is composed at
the service boundary (it needs the store and the terminal), and the hook spool is not
per-provider wiring; the verticals decide what actually rides here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.adapters.agents.claude_sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.codex.usage import CodexUsageReader
from remote_agents.adapters.agents.codex_sessions import CodexAppServerClient, CodexSessionCatalogue
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.adapters.agents.cursor_sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.opencode.usage import OpenCodeUsageReader
from remote_agents.adapters.agents.opencode_sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, UsageQuery
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
                answers.append(AgentLimits(profile))
        return tuple(answers)

    def read(self, query: UsageQuery) -> AgentUsage | None:
        reader = self._by_profile.get(query.profile_id)
        if reader is None:
            return None
        try:
            return reader.read(query)  # type: ignore[attr-defined]
        except (OSError, ValueError, ArithmeticError, sqlite3.Error):
            return None


def _claude_sessions(project_paths: ProjectPaths) -> ClaudeSessionCatalogue:
    return ClaudeSessionCatalogue(project_paths)


def _codex_sessions(project_paths: ProjectPaths) -> CodexSessionCatalogue:
    return CodexSessionCatalogue(project_paths, CodexAppServerClient())


def _opencode_sessions(project_paths: ProjectPaths) -> OpenCodeSessionCatalogue:
    return OpenCodeSessionCatalogue(project_paths, OpenCodeCliRunner())


def _cursor_sessions(project_paths: ProjectPaths) -> CursorSessionCatalogue:  # noqa: ARG001
    # Cursor's catalogue is workspace-blind; the parameter keeps the factory contract one
    # shape for every provider.
    return CursorSessionCatalogue()


def provider_descriptors(
    *,
    claude_context_window: int | None = None,
    claude_context_window_stated: bool = False,
) -> tuple[ProviderDescriptor, ...]:
    """One descriptor per provider, in stable UI order.

    The two keyword arguments exist because exactly one capability is owner-configurable:
    Claude's context-window ceiling reaches its reader only when the owner stated one
    (DEC-061 — a reader supplying its own ceiling would be inventing it).
    """
    return (
        ProviderDescriptor(
            ProfileId("claude"),
            sessions=_claude_sessions,
            usage=ClaudeUsageReader(
                context_window=claude_context_window,
                context_window_stated=claude_context_window_stated,
            ),
            hooks="claude",
        ),
        ProviderDescriptor(
            ProfileId("codex"),
            sessions=_codex_sessions,
            usage=CodexUsageReader(),
            hooks="codex",
        ),
        ProviderDescriptor(
            ProfileId("opencode"),
            sessions=_opencode_sessions,
            usage=OpenCodeUsageReader(),
        ),
        ProviderDescriptor(
            ProfileId("cursor-agent"),
            sessions=_cursor_sessions,
            # Constant-empty, and deliberately not None: cursor answers "I publish nothing",
            # which renders as "not reported by this agent". A None here would make every
            # cursor session read "no conversation matched yet" — the temporary state — and
            # DEC-061 requires those two never conflate (usage.py's CursorUsageReader
            # docstring records the same warning).
            usage=CursorUsageReader(),
        ),
    )


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
