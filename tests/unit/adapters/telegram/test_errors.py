"""Safe, actionable Telegram error mapping without diagnostic disclosure."""

from __future__ import annotations

import pytest

from remote_agents.adapters.telegram.errors import ErrorKind, render_error
from remote_agents.adapters.telegram.presenters import NavigationCallbacks

CALLBACKS = NavigationCallbacks(
    home="c1_home", back="c1_back", refresh="c1_refresh", previous="c1_previous", next="c1_next"
)


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        (ErrorKind.PROFILE_UNAVAILABLE, "This profile is currently unavailable."),
        (ErrorKind.INVALID_PROJECT, "This project is no longer available."),
        (ErrorKind.REGISTRY_DEGRADED, "The project catalogue is temporarily unavailable."),
        (ErrorKind.CONFLICT, "A conflicting operation is already in progress."),
        (ErrorKind.DUPLICATE_REQUEST, "This request was already handled."),
        (ErrorKind.DATABASE_UNAVAILABLE, "Session storage is temporarily unavailable."),
        (ErrorKind.TERMINAL_UNAVAILABLE, "The managed terminal is temporarily unavailable."),
        (ErrorKind.UNEXPECTED, "The request could not be completed."),
    ),
)
def test_each_error_is_truthful_safe_and_offers_recovery(kind: ErrorKind, expected: str) -> None:
    rendered = render_error(kind, CALLBACKS, diagnostic="/home/user/private token=secret traceback")

    assert rendered.text == expected
    assert [button.text for row in rendered.keyboard for button in row] == ["Refresh", "Home"]
    assert "private" not in rendered.text
    assert "secret" not in rendered.text
    assert "success" not in rendered.text.casefold()
