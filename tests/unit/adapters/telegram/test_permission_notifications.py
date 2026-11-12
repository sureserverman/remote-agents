"""The bot's one message for a session whose permission preset could not be applied."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from telegram.constants import ParseMode

from remote_agents.adapters.telegram.binding_notifications import BindingNotifier
from remote_agents.application.permission_apply import permissions_notice
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


class _View:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_apart(self, bot, message) -> None:
        self.sent.append(message)


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("p1"),
        ProfileId("codex"),
        SessionDisplayIdentity("p1", "codex", "regular", 4),
        SessionState.RUNNING,
        datetime(2026, 11, 12, tzinfo=UTC),
    )


async def test_a_permission_failure_is_one_message_with_the_shared_sentence_escaped() -> None:
    view = _View()
    notifier = BindingNotifier(view=view, project_name=lambda project: "R&D <tools>")
    notifier.attach(object())
    record = _record()
    reason = "thread/settings/update refused <b>(code -32600)</b>"

    await notifier.notify_permissions(record, reason)
    await notifier.pass_once()

    assert len(view.sent) == 1
    (message,) = view.sent
    assert message["parse_mode"] == ParseMode.HTML
    said = permissions_notice(record, "R&D <tools>", reason)
    assert "#4" in said and reason in said
    # The application's sentence, with the project's name and the daemon's words as data.
    assert message["text"] == escape(said)
    assert "R&amp;D &lt;tools&gt;" in message["text"]
    assert "&lt;b&gt;(code -32600)&lt;/b&gt;" in message["text"]
    assert "<" not in message["text"]


async def test_a_permission_notice_is_held_until_the_bot_attaches() -> None:
    view = _View()
    notifier = BindingNotifier(view=view, project_name=lambda project: "p")

    await notifier.notify_permissions(_record(), "timed out")
    assert view.sent == [] and len(notifier.pending) == 1

    notifier.attach(object())
    await notifier.pass_once()
    assert len(view.sent) == 1 and notifier.pending == ()
