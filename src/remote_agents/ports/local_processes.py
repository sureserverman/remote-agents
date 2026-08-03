"""Read-only local-process discovery boundary for safe handoff candidates."""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.external_sessions import (
    ExternalSessionReference,
    ExternalSessionSummary,
    ResolvedExternalSession,
)


class LocalProcessCatalog(Protocol):
    """Exposes bounded, content-free evidence without any unmanaged-process control."""

    async def list_external_sessions(
        self, *, excluded_process_roots: tuple[int, ...] = ()
    ) -> tuple[ExternalSessionSummary, ...]: ...
    async def resolve_external_session(
        self, reference: ExternalSessionReference
    ) -> ResolvedExternalSession | None: ...
    async def is_still_running(self, reference: ExternalSessionReference) -> bool: ...
