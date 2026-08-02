from remote_agents.adapters.tmux.remote_control import (
    REMOTE_CONTROL_DISCONNECT_KEYS,
    REMOTE_CONTROL_ENABLE_KEYS,
    RemoteControlState,
    classify_remote_control_capture,
)


def test_remote_control_enable_and_disconnect_interactions_are_fixed() -> None:
    assert REMOTE_CONTROL_ENABLE_KEYS == ("/remote-control", "Enter")
    assert REMOTE_CONTROL_DISCONNECT_KEYS == ("Up", "Up", "Enter")


def test_capture_classification_uses_the_latest_known_transition() -> None:
    capture = "/remote-control is active\nRemote Control disconnected.\n"

    assert classify_remote_control_capture(capture) is RemoteControlState.INACTIVE


def test_capture_classification_fails_closed_for_unknown_output() -> None:
    assert classify_remote_control_capture("Claude Code") is RemoteControlState.UNKNOWN
