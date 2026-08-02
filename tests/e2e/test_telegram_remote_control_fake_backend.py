from remote_agents.adapters.tmux.runtime import _remote_control_state
from remote_agents.domain.remote_control import RemoteControlState


def test_remote_control_fake_backend_fails_closed_for_unclassified_capture():
    assert _remote_control_state("unclassified") is RemoteControlState.UNKNOWN
