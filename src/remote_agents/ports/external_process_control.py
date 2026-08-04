"""Typed boundary for the one fixed external-process mutation."""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.external_sessions import ExternalProcessIdentity, ExternalStopResult


class ExternalProcessController(Protocol):
    """Terminates exactly one revalidated external process using fixed SIGTERM semantics."""

    async def terminate(self, identity: ExternalProcessIdentity) -> ExternalStopResult: ...
    async def is_gone(self, identity: ExternalProcessIdentity) -> bool: ...
