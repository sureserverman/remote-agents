from remote_agents.adapters.tmux.remote_control import (
    REMOTE_CONTROL_DISCONNECT_KEYS,
    REMOTE_CONTROL_ENABLE_KEYS,
)
from remote_agents.adapters.tmux.runtime import _remote_control_state
from remote_agents.domain.remote_control import RemoteControlState


def test_remote_control_uses_only_the_qualified_fixed_sequences_and_markers():
    assert REMOTE_CONTROL_ENABLE_KEYS == ("/remote-control", "Enter")
    assert REMOTE_CONTROL_DISCONNECT_KEYS == ("Up", "Up", "Enter")
    assert _remote_control_state("/remote-control is active") is RemoteControlState.ACTIVE
    assert _remote_control_state("Remote Control disconnected.") is RemoteControlState.INACTIVE
