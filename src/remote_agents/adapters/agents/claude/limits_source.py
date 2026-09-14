"""Claude's limits, from whichever source the owner's switch names -- consulted per read.

Two readers answer the same question: the status-line hop's recording
(`adapters.agents.claude.usage.ClaudeUsageReader`) and, opt-in, the usage API
(`adapters.agents.claude.usage_api.ClaudeUsageApiReader`), which keeps the hop as its own
fallback. This is the one object the descriptor registers as Claude's `usage` capability
(DEC-070 -- one package, one descriptor field, one registry entry), and it decides between the
two on every account read by asking the switch, so the owner flipping
`limits.claude_limits_source` needs no restart and no recomposition.

The switch is a callable the composition root hands in (DEC-046), not a file this package
opens: an adapter may not import `remote_agents.config`, and the file's location is the root's
to know. Anything but a value in the closed set -- a callable that raises, an empty string, a
word this build does not know -- routes to the hop, which is the default and grants nothing
new; the API is only ever reached by the switch saying so (DEC-061, amended).

A session's usage is not a question the switch bears on: `read` goes to the hop reader, which
is the transcript reader, untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, UsageQuery

#: The switch value that routes to the usage API; every other value is the hop. Spelled here
#: rather than imported from `config` because an adapter may not import that module; that it
#: is a member of the config's closed set is pinned by
#: `test_the_selector_literal_for_claude_limits_source_is_in_the_closed_set`.
USAGE_API = "usage-api"


class LimitsReader(Protocol):
    def limits(self) -> AgentLimits: ...


class SessionReader(Protocol):
    def limits(self) -> AgentLimits: ...

    def read(self, query: UsageQuery) -> AgentUsage | None: ...


class ClaudeLimitsSource:
    """Route the account read by the switch; hand the session read to the hop reader."""

    profiles = frozenset({ProfileId("claude")})

    limits_profile = ProfileId("claude")

    def __init__(
        self, read_switch: Callable[[], str], api_reader: LimitsReader, hop_reader: SessionReader
    ) -> None:
        self._read_switch = read_switch
        self._api = api_reader
        self._hop = hop_reader

    def read(self, query: UsageQuery) -> AgentUsage | None:
        return self._hop.read(query)

    def limits(self) -> AgentLimits:
        try:
            chosen = self._read_switch()
        except Exception:  # noqa: BLE001 -- a switch that cannot be read is the default, never a screen down
            chosen = None
        if chosen == USAGE_API:
            return self._api.limits()
        return self._hop.limits()
