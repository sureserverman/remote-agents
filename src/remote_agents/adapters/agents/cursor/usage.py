"""Cursor's usage read: a constant, honest "I publish nothing"."""

from __future__ import annotations

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, UsageQuery


class CursorUsageReader:
    """Answer, honestly and immediately, that cursor-agent publishes nothing to read.

    This is a real reader and not an omission. `~/.cursor/chats/<workspace>/<chat>/store.db`
    holds the conversation itself — system prompt, turns, protobuf-encoded metadata — and no
    accounting of any kind; every one of the 255 stores on this host was searched for a token
    or context field on 2026-08-27 and none carries one. `cursor-agent about` reports the
    subscription tier and no usage against it, and the CLI exposes no usage subcommand.

    The one place Cursor does emit a context window is the payload it pushes to a *status line*
    command, which is a push into a process Cursor starts — not something a third party can
    read, and reachable only by installing a status line of this project's own over the one the
    owner already has.

    So the empty `AgentUsage` returned here is the accurate answer rather than a placeholder,
    and presentation renders it as "not reported by this agent". Deleting this class in favour
    of no reader at all would render the *other* sentence — "no conversation matched" — which
    invites the owner to wait for a number that is never coming.
    """

    profiles = frozenset({ProfileId("cursor-agent")})

    limits_profile = ProfileId("cursor-agent")

    def read(self, query: UsageQuery) -> AgentUsage:  # noqa: ARG002 - the answer is constant
        return AgentUsage()

    def limits(self) -> AgentLimits:
        """Constant for the reason `read` is: there is nothing on disk to consult."""
        return AgentLimits(self.limits_profile)
