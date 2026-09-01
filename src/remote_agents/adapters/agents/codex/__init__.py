"""Codex's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.codex.sessions import CodexAppServerClient, CodexSessionCatalogue
from remote_agents.adapters.agents.codex.usage import CodexUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CodexSessionCatalogue:
    return CodexSessionCatalogue(project_paths, CodexAppServerClient())


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04)."""
    return ProviderDescriptor(
        ProfileId("codex"),
        sessions=_sessions,
        usage=CodexUsageReader(),
        hooks="codex",
    )
