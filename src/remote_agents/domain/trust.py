"""Typed state for the folder-trust question a managed launch can block on."""

from enum import StrEnum


class TrustState(StrEnum):
    AWAITING = "awaiting"
    UNKNOWN = "unknown"
