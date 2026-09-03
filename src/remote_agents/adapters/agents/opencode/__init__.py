"""OpenCode's provider vertical: sessions, usage, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.opencode.sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.adapters.agents.opencode.usage import OpenCodeUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor


def _sessions(project_paths: Mapping[ProjectId, Path]) -> OpenCodeSessionCatalogue:
    return OpenCodeSessionCatalogue(project_paths, OpenCodeCliRunner())


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Hooks and `remote_control` both stay a declared None: opencode takes no hooks and
    publishes no host-level Remote Control (DEC-061).
    """
    return ProviderDescriptor(
        ProfileId("opencode"),
        sessions=_sessions,
        usage=OpenCodeUsageReader(),
    )
