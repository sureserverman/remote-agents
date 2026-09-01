"""OpenCode's usage read: the opencode.db message table's token accounting."""

from __future__ import annotations

import sqlite3
from datetime import UTC
from pathlib import Path

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    ContextWindow,
    UsageQuery,
)
from remote_agents.ports.agent_usage_support import (
    _START_TOLERANCE,
    _loads,
    _moment,
    _positive_int,
    _resolved,
)


class OpenCodeUsageReader:
    """Read one OpenCode session's context out of the single SQLite database it keeps.

    OpenCode publishes no rate-limit information, so this answers a context window and an empty
    window tuple — which `AgentUsage` distinguishes from "no answer" precisely so a reader like
    this one does not have to pretend the two are the same.

    The database is opened read-only through a URI, and never with a fallback to read-write:
    this is another program's live database, with a WAL beside it, and the one guarantee worth
    making about touching it is that this cannot be the process that writes to it.
    """

    profiles = frozenset({ProfileId("opencode")})

    limits_profile = ProfileId("opencode")

    def limits(self) -> AgentLimits:
        """OpenCode publishes no rate limits, which is an answer and not a gap."""
        return AgentLimits(self.limits_profile)

    def __init__(self, *, database: Path | None = None, now: object = None) -> None:
        self._database = database or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        self._now = now

    def read(self, query: UsageQuery) -> AgentUsage | None:
        row = self._newest_assistant(query)
        if row is None:
            return None
        tokens = row.get("tokens") if isinstance(row, dict) else None
        if not isinstance(tokens, dict):
            return AgentUsage()
        total = _positive_int(tokens.get("total"))
        if total is None:
            return AgentUsage()
        return AgentUsage(context=ContextWindow(total), observed_at=_moment(self._now))

    def _newest_assistant(self, query: UsageQuery) -> dict | None:
        workspace = str(_resolved(query.workspace))
        floor = int((query.started_at.astimezone(UTC) - _START_TOLERANCE).timestamp() * 1000)
        statement = (
            "SELECT m.data FROM message AS m JOIN session AS s ON s.id = m.session_id "
            "WHERE s.directory = ? AND m.time_created >= ? "
            "ORDER BY m.time_created DESC LIMIT 40"
        )
        parameters: tuple[object, ...] = (workspace, floor)
        if query.resume_source_id is not None:
            statement = (
                "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 40"
            )
            parameters = (query.resume_source_id,)
        try:
            connection = sqlite3.connect(f"file:{self._database}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        for (data,) in rows:
            document = _loads(data)
            if isinstance(document, dict) and document.get("role") == "assistant":
                return document
        return None
