"""Typed state for the folder-trust question a managed launch can block on."""

from enum import StrEnum

from remote_agents.domain.models import ProfileId


class TrustState(StrEnum):
    AWAITING = "awaiting"
    UNKNOWN = "unknown"


#: The single authority on which profiles can be asked the trust question, and it lives in
#: the domain because both the application policy and the tmux adapter must agree on it and
#: neither may import the other (DEC-001). Duplicating it is how it came to be wrong in three
#: places at once -- the policy said one thing, the runtime another, and the service a third,
#: so the button rendered and then refused itself when pressed.
#:
#: Both spellings of Claude: `claude-remote` is `claude --remote-control`, the same binary
#: showing the same dialog.
TRUST_ANSWERABLE = frozenset({ProfileId("claude"), ProfileId("claude-remote")})
