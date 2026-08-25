"""Health and operational events remain truthful without carrying protected content."""

from __future__ import annotations

import json

import pytest

from remote_agents.application.health import health_report, structured_event


@pytest.mark.parametrize(
    ("component", "reason"),
    [
        ("telegram", "offline"),
        ("registry", "unavailable"),
        ("profiles", "no_profile_available"),
        ("tmux", "unavailable"),
        ("database", "unavailable"),
        ("sessions", "orphaned"),
        ("reconciler", "failed"),
        ("service", "shutting_down"),
    ],
)
def test_each_degraded_component_is_reported_truthfully(component: str, reason: str) -> None:
    report = health_report(
        {
            "core": (True, None),
            "telegram": (True, None),
            "registry": (True, None),
            "profiles": (True, None),
            "tmux": (True, None),
            "database": (True, None),
            "sessions": (True, None),
            "reconciler": (True, None),
            "service": (True, None),
            component: (False, reason),
        }
    )

    assert report["healthy"] is False
    assert report["components"][component] == {"status": "degraded", "reason": reason}


def test_healthy_components_and_structured_events_are_machine_readable() -> None:
    report = health_report({"core": (True, None), "service": (True, None)})
    event = structured_event("service", "shutdown", "complete")

    assert report["healthy"] is True
    assert json.loads(event) == {"component": "service", "event": "shutdown", "status": "complete"}


@pytest.mark.parametrize("unsafe", ["token_value", "pane_output", "raw callback", "environment"])
def test_health_events_refuse_secret_or_content_bearing_fields(unsafe: str) -> None:
    with pytest.raises(ValueError, match="content-free"):
        structured_event("service", "failure", "degraded", reason=unsafe)
