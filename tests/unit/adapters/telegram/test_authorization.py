"""Authorization rejects untrusted updates before callback parsing or service access."""

import pytest

from remote_agents.adapters.telegram.authorization import (
    AuthorizationGate,
    AuthorizationUpdate,
    ContentFreeDenialLog,
)


def allowed_update() -> AuthorizationUpdate:
    return AuthorizationUpdate(sender_id=7, chat_id=11, chat_type="private", kind="callback")


def test_owner_in_private_chat_reaches_the_parser_once() -> None:
    parser_calls: list[str] = []
    gate = AuthorizationGate(7, 11, ContentFreeDenialLog())

    accepted = gate.dispatch(allowed_update(), lambda: parser_calls.append("parsed"))

    assert accepted is True
    assert parser_calls == ["parsed"]


@pytest.mark.parametrize(
    "update",
    (
        AuthorizationUpdate(sender_id=8, chat_id=11, chat_type="private", kind="callback"),
        AuthorizationUpdate(sender_id=7, chat_id=12, chat_type="private", kind="callback"),
        AuthorizationUpdate(sender_id=7, chat_id=11, chat_type="group", kind="callback"),
        AuthorizationUpdate(sender_id=None, chat_id=11, chat_type="private", kind="edited"),
        AuthorizationUpdate(sender_id=None, chat_id=None, chat_type=None, kind="channel"),
        AuthorizationUpdate(sender_id=7, chat_id=11, chat_type="private", kind="malformed"),
    ),
)
def test_denied_update_never_reaches_parser_and_logs_no_content(
    update: AuthorizationUpdate,
) -> None:
    parser_calls: list[str] = []
    denials = ContentFreeDenialLog(limit=1)
    gate = AuthorizationGate(7, 11, denials)

    accepted = gate.dispatch(update, lambda: parser_calls.append("parsed"))

    assert accepted is False
    assert parser_calls == []
    assert denials.events == ("denied",)


def test_denials_are_rate_limited_without_retaining_update_content() -> None:
    denials = ContentFreeDenialLog(limit=1)
    gate = AuthorizationGate(7, 11, denials)
    denied = AuthorizationUpdate(sender_id=8, chat_id=11, chat_type="private", kind="callback")

    gate.dispatch(denied, lambda: None)
    gate.dispatch(denied, lambda: None)

    assert denials.events == ("denied",)
