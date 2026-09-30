"""Cursor's limits, read only when the owner's switch says so -- consulted per read.

`CursorUsageApiReader` reads the Cursor CLI's login and calls Cursor's server, which DEC-061
and DEC-087 make opt-in. This router is what the descriptor registers as Cursor's `usage`
capability when a switch is wired (DEC-070 -- one package, one descriptor field), and it asks
the switch on every account read, so the owner flipping `limits.cursor_limits_source` needs no
restart.

The switch is a callable the composition root hands in (DEC-046): an adapter may not import
`remote_agents.config`. Only the one literal below reaches the API. Every other answer -- a
callable that raises, an empty string, a word this build does not know -- is off, and off
makes no call and opens no file.

Off answers `NOT_REPORTED` with `LimitsNote.OFF`. Cursor still publishes nothing this project
may read without the owner's say, which is `NOT_REPORTED`; the note says a switch would change
that, which the bare answer from `CursorUsageReader` never claims.

A session's usage is not a question the switch bears on: `read` goes to the constant reader.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    LimitsAbsence,
    LimitsNote,
    UsageQuery,
)

#: The switch value that routes to the usage API; every other value is off. Spelled here
#: rather than imported from `config` because an adapter may not import that module; that it
#: is a member of the config's closed set is pinned by
#: `test_the_selector_literal_for_cursor_limits_source_is_in_the_closed_set`.
USAGE_API = "usage-api"


class LimitsReader(Protocol):
    def limits(self) -> AgentLimits: ...


class SessionReader(Protocol):
    def read(self, query: UsageQuery) -> AgentUsage | None: ...


class CursorLimitsSource:
    """Route the account read by the switch; hand the session read to the constant reader."""

    profiles = frozenset({ProfileId("cursor-agent")})

    limits_profile = ProfileId("cursor-agent")

    def __init__(
        self,
        read_switch: Callable[[], str],
        api_reader: LimitsReader,
        session_reader: SessionReader,
    ) -> None:
        self._read_switch = read_switch
        self._api = api_reader
        self._sessions = session_reader

    def read(self, query: UsageQuery) -> AgentUsage | None:
        return self._sessions.read(query)

    def limits(self) -> AgentLimits:
        try:
            chosen = self._read_switch()
        except Exception:  # noqa: BLE001 -- a switch that cannot be read is off, never a screen down
            chosen = None
        if isinstance(chosen, str) and chosen == USAGE_API:
            return self._api.limits()
        return AgentLimits(
            self.limits_profile, absence=LimitsAbsence.NOT_REPORTED, note=LimitsNote.OFF
        )
