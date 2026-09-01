"""Claude's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.claude.sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor


def _sessions(project_paths: Mapping[ProjectId, Path]) -> ClaudeSessionCatalogue:
    return ClaudeSessionCatalogue(project_paths)


def descriptor(
    *, context_window: int | None = None, context_window_stated: bool = False
) -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    The two keyword arguments exist because exactly one capability is owner-configurable:
    the context-window ceiling reaches the reader only when the owner stated one (DEC-061 —
    a reader supplying its own ceiling would be inventing it).
    """
    return ProviderDescriptor(
        ProfileId("claude"),
        sessions=_sessions,
        usage=ClaudeUsageReader(
            context_window=context_window, context_window_stated=context_window_stated
        ),
        hooks="claude",
    )
