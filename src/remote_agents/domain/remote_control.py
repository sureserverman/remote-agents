"""Typed state for the closed Claude Remote Control lifecycle action."""

from enum import StrEnum


class RemoteControlState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"
