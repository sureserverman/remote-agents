"""Codex's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.codex.remote_control import CodexRemoteControl
from remote_agents.adapters.agents.codex.sessions import (
    CodexAppServerClient,
    CodexSessionCatalogue,
)
from remote_agents.adapters.agents.codex.usage import CodexUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CodexSessionCatalogue:
    return CodexSessionCatalogue(project_paths, CodexAppServerClient())


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Codex is the one provider wiring `remote_control`: its Remote Control is a property of
    the shared app-server daemon this machine runs, which is a host fact with no session to
    hang off. Constructed here with its default collaborators; everything below
    `tests/live` injects its own, so nothing but the live drill runs a real `codex`.
    """
    return ProviderDescriptor(
        ProfileId("codex"),
        sessions=_sessions,
        usage=CodexUsageReader(),
        hooks="codex",
        remote_control=CodexRemoteControl(),
    )
