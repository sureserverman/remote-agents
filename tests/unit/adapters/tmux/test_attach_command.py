"""Copyable tmux attachment is constrained to one exact managed session."""

import pytest

from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.domain.models import SessionId


def test_attach_command_uses_the_production_dedicated_socket_and_exact_target() -> None:
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")

    assert attach_command(session_id) == (
        "tmux -L remote-agents attach-session -t ra-01234567-89ab-cdef-0123-456789abcdef:"
    )


def test_attach_command_accepts_only_a_typed_managed_session_id() -> None:
    with pytest.raises(ValueError):
        SessionId.parse("not-a-session")
