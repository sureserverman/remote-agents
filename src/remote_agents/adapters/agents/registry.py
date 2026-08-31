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

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.claude_sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.codex_sessions import CodexAppServerClient, CodexSessionCatalogue
from remote_agents.adapters.agents.cursor_sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.opencode_sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.adapters.agents.usage import (
    ClaudeUsageReader,
    CodexUsageReader,
    CursorUsageReader,
    OpenCodeUsageReader,
    ProfileUsageReaders,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor

ProjectPaths = Mapping[ProjectId, Path]


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
