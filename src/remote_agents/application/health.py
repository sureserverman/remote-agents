"""Content-free health and event records for local operator diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

_CODE = re.compile(r"[a-z0-9_]+")
_FORBIDDEN = ("token", "environment", "prompt", "pane", "callback")


def health_report(components: Mapping[str, tuple[bool, str | None]]) -> dict[str, object]:
    """Report each observed dependency without upgrading a degraded state to healthy."""
    rendered = {
        name: {
            "status": "healthy" if available else "degraded",
            "reason": None if available else _safe_code(reason or "unavailable"),
        }
        for name, (available, reason) in components.items()
    }
    return {
        "healthy": all(component["status"] == "healthy" for component in rendered.values()),
        "components": rendered,
    }


def structured_event(component: str, event: str, status: str, *, reason: str | None = None) -> str:
    """Serialize an operator event while rejecting every content-bearing field."""
    document: dict[str, str] = {
        "component": _safe_code(component),
        "event": _safe_code(event),
        "status": _safe_code(status),
    }
    if reason is not None:
        document["reason"] = _safe_code(reason)
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _safe_code(value: str) -> str:
    if not _CODE.fullmatch(value) or any(forbidden in value for forbidden in _FORBIDDEN):
        raise ValueError("health fields must be content-free diagnostic codes")
    return value
