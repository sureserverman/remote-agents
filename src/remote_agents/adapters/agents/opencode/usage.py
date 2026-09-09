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
    LimitsAbsence,
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

    OpenCode does not publish rate-limit information, so this answers a context window and an empty
    window tuple — which `AgentUsage` distinguishes from "no answer" precisely so a reader like
    this one does not have to pretend the two are the same.

    The database is opened read-only through a URI, and never with a fallback to read-write:
    this is another program's live database, with a WAL beside it, and the one guarantee worth
    making about touching it is that this cannot be the process that writes to it.
    """

    profiles = frozenset({ProfileId("opencode")})

    limits_profile = ProfileId("opencode")

    def limits(self) -> AgentLimits:
        """OpenCode does not publish rate limits, which is an answer and not a gap.

        Named `NOT_REPORTED` so the gap has a word: permanent, complete, and distinct from the
        silence of a provider that does publish limits and had none to give today (DEC-061).
        """
        return AgentLimits(self.limits_profile, absence=LimitsAbsence.NOT_REPORTED)

    def __init__(self, *, database: Path | None = None, now: object = None) -> None:
        self._database = database or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        self._now = now

    def read(self, query: UsageQuery) -> AgentUsage | None:
        """This session's context, from the newest assistant message that counted any.

        **The newest message that carries a count, rather than the newest message.** OpenCode
        writes an assistant row as a turn opens and fills its `tokens` in as the turn runs, so
        the newest row is routinely a placeholder — `{"input": 0, "output": 0, "reasoning": 0,
        "cache": {...}}`, with no `total` key at all. Reading only that row reported "not
        reported by this agent" for a session that had counted every turn it ever took, and it
        did so *while the agent was working*, which is exactly when the owner looks. Found on a
        live host whose newest row was that placeholder and whose next one held 10285.

        Skipping such a row is not the same as inventing a reading: a turn that has produced no
        count yet has not produced one, and the previous turn's total is the last thing this
        conversation actually measured. The three outcomes `AgentUsage` distinguishes are all
        still reachable and still mean what they meant — no assistant message at all is `None`
        (no conversation matched), assistant messages that have *never* carried a total is the
        empty reading (matched, publishes nothing), and anything else is the newest count.
        """
        documents = self._assistant_messages(query)
        if not documents:
            return None
        total = next(
            (counted for counted in map(_message_total, documents) if counted is not None),
            None,
        )
        if total is None:
            return AgentUsage()
        return AgentUsage(context=ContextWindow(total), observed_at=_moment(self._now))

    def _assistant_messages(self, query: UsageQuery) -> list[dict]:
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
        documents = []
        for (data,) in rows:
            document = _loads(data)
            if isinstance(document, dict) and document.get("role") == "assistant":
                documents.append(document)
        return documents


def _message_total(document: dict) -> int | None:
    """The total this message counted, or None if it counted nothing.

    `None` covers both shapes the placeholder takes -- a `tokens` that is not a mapping, and
    one that is but has no positive `total` -- because the caller does the same thing with
    them: keep looking at older messages.
    """
    tokens = document.get("tokens")
    if not isinstance(tokens, dict):
        return None
    return _positive_int(tokens.get("total"))
