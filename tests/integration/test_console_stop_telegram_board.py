"""A console or Telegram stop retires one alert across the next service pass and restart."""

from __future__ import annotations

import json
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from backends import backend_for
from fake_telegram import FakeChat
from telegram.error import BadRequest

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.database import open_database, open_ui_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.standing_notification_store import (
    SQLiteStandingNotificationStore,
)
from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import SessionsPane
from remote_agents.adapters.tui.screens.confirm import ForceConfirmModal
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.composition.service import (
    ServiceComposition,
    _watch_activity_once,
    live_session_check,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.domain.state_machine import LifecycleEvent

_PROJECT = CatalogProject("demo", "demo", "infra", "Demo")


def _spool(directory, session, detail):
    (directory / f"{session.session_id}-20261125T110000000001Z.json").write_text(
        json.dumps(
            {
                "session_id": str(session.session_id),
                "observed_at": datetime.now(UTC).isoformat(),
                "event": "Stop",
                "kind": "completed",
                "confidence": "reported",
                "detail": detail,
            }
        ),
        encoding="utf-8",
    )


def _button(message, label):
    matched = [
        button.callback_data
        for row in message.reply_markup.inline_keyboard
        for button in row
        if unpadded(button.text).endswith(label)
    ]
    assert len(matched) == 1, (label, message)
    return matched[0]


def _board(chat, ui, domain, terminal, spool):
    sessions = SQLiteSessionStore(domain)
    boundary = build_private_bot(
        chat.owner_id,
        chat.chat_id,
        callbacks=SQLiteCallbackStateStore(ui),
        anchors=SQLiteChatViewStore(ui),
        standing=SQLiteStandingNotificationStore(ui),
        backend=backend_for(sessions=SessionService(sessions, terminal), catalogue=(_PROJECT,)),
    )
    boundary.notifier.attach(chat.bot)
    boundary.attach_bot(chat.bot)
    return ServiceComposition(
        boundary,
        terminal,
        None,
        activity_directory=spool,
        activity_store=SQLiteActivityStore(domain),
        live_session=live_session_check(sessions),
    )


async def _console_stop(route, domain, terminal, target, other):
    app = SessionsPane(
        TuiContext(
            backend=backend_for(
                sessions=SessionService(SQLiteSessionStore(domain), terminal),
                projects=object(),
                refresh_catalogue=lambda: (_PROJECT,),
                catalogue=(_PROJECT,),
            ),
            profiles=(),
            attach_argv=None,
            console_holds_slot=AsyncMock(return_value=True),
            console_displayed_session=AsyncMock(return_value=target.session_id),
            console_visible_selection=AsyncMock(return_value=other.session_id),
        )
    )
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press(route)
        if route == "f9":
            await pilot.pause()
            assert isinstance(app.screen, ForceConfirmModal)
            await pilot.press("down", "enter")
        await pilot.pause()


@pytest.mark.parametrize("route", ["f8", "f9", "telegram"])
async def test_only_the_stopped_sessions_alert_retires_across_restart(tmp_path, route, monkeypatch):
    chat = FakeChat()
    terminal = FakeTerminal()
    spool = tmp_path / "activity"
    spool.mkdir()
    database = tmp_path / "domain.sqlite3"
    ui_database = tmp_path / "ui.sqlite3"
    with ExitStack() as stack:
        console_db = stack.enter_context(closing(open_database(database)))
        console_sessions = SQLiteSessionStore(console_db)
        launcher = SessionService(console_sessions, terminal)
        a, b = [
            (
                await launcher.launch(
                    LaunchCommand(ProjectId("demo"), ProfileId("claude"), tag, tag)
                )
            ).record
            for tag in ("Target A", "Survivor B")
        ]

        with (
            closing(open_database(database)) as domain,
            closing(open_ui_database(ui_database)) as ui,
        ):
            service = _board(chat, ui, domain, terminal, spool)
            bot = service.boundary
            await bot.start(chat.message_update("/start"), None)
            _spool(spool, a, "Result from target A.")
            _spool(spool, b, "Result from survivor B.")
            await _watch_activity_once(service)
            menu = bot.view.anchor()
            (a_alert,) = [m for m in chat.bot_messages if "Result from target A." in m.text]
            (b_alert,) = [m for m in chat.bot_messages if "Result from survivor B." in m.text]
            assert {m.message_id for m in chat.bot_messages} == {
                menu,
                a_alert.message_id,
                b_alert.message_id,
            }
            b_open = _button(b_alert, "Open session")

            # Return the precise refusal LiveView turns into False. On the old code
            # this discards the standing row while the obsolete keyboard survives.
            original_delete = chat.bot.delete_message

            async def refuse_old_alert(*, chat_id, message_id):
                if message_id == a_alert.message_id:
                    raise BadRequest("Message can't be deleted")
                return await original_delete(chat_id=chat_id, message_id=message_id)

            monkeypatch.setattr(chat.bot, "delete_message", refuse_old_alert)
            if route == "telegram":
                picker = _button(chat.messages[menu], f"#{a.display.sequence} {_PROJECT.name}")
                await bot.callback(chat.press(picker), None)
                assert "Target A" in chat.messages[menu].text
                await bot.callback(chat.press(_button(chat.messages[menu], "Stop and close")), None)
            else:
                await _console_stop(route, console_db, terminal, a, b)
            _spool(spool, a, "Late report after the stop.")
            await _watch_activity_once(service)
            monkeypatch.setattr(chat.bot, "delete_message", original_delete)

        # A fresh bot, session service, callback store and notifier reopen the files.
        with (
            closing(open_database(database)) as domain,
            closing(open_ui_database(ui_database)) as ui,
        ):
            service = _board(chat, ui, domain, terminal, spool)
            bot = service.boundary
            await _watch_activity_once(service)
            assert (await console_sessions.get(a.session_id)).state is SessionState.ENDED
            assert (await console_sessions.get(b.session_id)).state is SessionState.RUNNING
            ending = (
                LifecycleEvent.VERIFIED_FORCE_STOP
                if route == "f9"
                else LifecycleEvent.CLEANUP_CONFIRMED
            )
            assert (await console_sessions.events(a.session_id))[-1].event_type == ending
            assert {m.message_id for m in chat.bot_messages if m.reply_markup} == {
                menu,
                b_alert.message_id,
            }, chat.transcript()
            assert not any("Late report" in m.text for m in chat.bot_messages)
            assert {
                (event.session_id, event.detail)
                for event in await service.activity_store.recent(limit=5)
            } == {
                (str(a.session_id), "Result from target A."),
                (str(b.session_id), "Result from survivor B."),
            }

            await bot.callback(chat.press(_button(chat.messages[menu], "Settings")), None)
            assert "Settings" in chat.messages[menu].text
            press = chat.press(b_open)
            await bot.callback(press, None)
            assert press.callback_query.answers == [None]
            assert "Survivor B" in chat.messages[menu].text
            assert b_alert.message_id not in chat.messages
