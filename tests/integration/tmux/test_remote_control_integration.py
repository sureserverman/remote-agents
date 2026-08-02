from remote_agents.adapters.tmux.runtime import _remote_control_state
from remote_agents.domain.remote_control import RemoteControlState


def test_unknown_capture_fails_closed_before_any_interaction_is_attempted():
    assert _remote_control_state("unrelated terminal output") is RemoteControlState.UNKNOWN
