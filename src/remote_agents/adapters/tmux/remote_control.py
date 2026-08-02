"""Fixed, profile-owned Claude Remote Control interaction and capture classification."""

from enum import StrEnum

REMOTE_CONTROL_ENABLE_KEYS = ("/remote-control", "Enter")
REMOTE_CONTROL_OPEN_MENU_KEYS = ("/remote-control", "Enter")
REMOTE_CONTROL_DISCONNECT_KEYS = ("Up", "Up", "Enter")


class RemoteControlState(StrEnum):
    """Only states that can be verified from bounded managed-pane capture."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


def classify_remote_control_capture(capture: str) -> RemoteControlState:
    """Use Claude's fixed status markers while honoring the latest observed transition."""
    active_at = max(
        capture.rfind("/remote-control is active"),
        capture.rfind("Disconnect this session"),
    )
    disconnected_at = capture.rfind("Remote Control disconnected.")
    if disconnected_at > active_at:
        return RemoteControlState.INACTIVE
    if active_at >= 0:
        return RemoteControlState.ACTIVE
    return RemoteControlState.UNKNOWN
