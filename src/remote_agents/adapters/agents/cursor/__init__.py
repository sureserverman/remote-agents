"""Cursor's provider vertical: sessions, usage, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.cursor.sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CursorSessionCatalogue:  # noqa: ARG001
    # Cursor's catalogue is workspace-blind; the parameter keeps the factory contract one
    # shape for every provider.
    return CursorSessionCatalogue()


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Hooks stay a declared None. `usage` is constant-empty and deliberately NOT None: cursor
    answers "I publish nothing", which renders as "not reported by this agent"; a None here
    would render "no conversation matched yet" forever (DEC-061 — the two must never
    conflate; the fold regression test pins the consequence).
    """
    return ProviderDescriptor(
        ProfileId("cursor-agent"),
        sessions=_sessions,
        usage=CursorUsageReader(),
    )
