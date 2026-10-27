"""Owner journeys preserve one shared default and an optional session override."""

from dataclasses import replace
from datetime import UTC, datetime
from html import unescape

import pytest
from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat
from textual.widgets import OptionList
from tui_feedback import announcements

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot, unmarked
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.model import LaunchSelection
from remote_agents.adapters.tui.screens.launch import ProfilesScreen
from remote_agents.adapters.tui.screens.resume import ResumeConversationsScreen
from remote_agents.application.permission_presets import (
    PERMISSION_CHOICES,
    PERMISSION_TITLE,
    PermissionSettings,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import LaunchOutcome, ResumeOutcome
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset

PROJECT = CatalogProject("a" * 24, "fixture", "infra", "Registered")
CODEX = ProfileId("codex")
SUMMARY = ConversationSummary(
    ConversationReference("c-" + "a" * 20),
    CODEX,
    ProjectId(PROJECT.opaque_id),
    ConversationState.RESUMABLE,
    datetime.now(UTC),
    description="saved work",
)
RESOLVED = ResolvedConversation(SUMMARY, ProviderConversationId("private-source"))


class Sessions(SessionUseCaseDouble):
    def __init__(self):
        self.commands = []
        self.record = SessionRecord(
            SessionId.new(),
            ProjectId(PROJECT.opaque_id),
            CODEX,
            SessionDisplayIdentity("fixture", "codex", "regular", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
        )

    async def list_sessions(self):
        return ()

    async def refresh_readiness(self):
        return ()

    async def launch(self, command):
        self.commands.append(command)
        return LaunchOutcome(self.record, remote_control=False)

    async def resume(self, command):
        self.commands.append(command)
        return ResumeOutcome(self.record, created=True)

    async def permission_preset(self, session_id):
        return PermissionPreset.ASK


class Conversations:
    async def catalogue(self, query):
        return ConversationCataloguePage((SUMMARY,), 1, 1)

    async def resolve_for_resume(self, reference):
        return RESOLVED if reference == SUMMARY.reference else None


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[codex]\ndefault_permissions = "approve_for_me"\n')
    return PermissionSettings(
        ConfigPermissionSetting(path), profiles={CODEX: tuple(PermissionPreset)}
    )


def backend(settings, sessions):
    return replace(
        backend_for(
            sessions=sessions,
            projects=object(),
            conversations=Conversations(),
            catalogue=(PROJECT,),
            refresh_catalogue=lambda: (PROJECT,),
        ),
        permission_presets=settings,
    )


def bot_for(settings, sessions=None):
    return build_private_bot(
        7,
        11,
        backend=backend(settings, sessions or Sessions()),
        profiles=(ProfileAvailability("codex", True),),
        names={"codex": "Codex"},
    )


def app_for(settings, sessions=None):
    async def opened(session):
        return None

    return RemoteAgentsTui(
        TuiContext(
            backend=backend(settings, sessions or Sessions()),
            profiles=(ProfileAvailability("codex", True),),
            attach_argv=lambda _: ("attach",),
            open_in_console=opened,
        )
    )


def button(message, prefix):
    for row in message.reply_markup.inline_keyboard:
        for item in row:
            label = unmarked(unpadded(item.text))
            if label.startswith(prefix):
                return item.callback_data
    raise AssertionError(f"no {prefix!r} button on {message.text!r}")


async def press(bot, chat, prefix):
    message = chat.bot_messages[-1]
    await bot.callback(chat.press(button(message, prefix), on=message.message_id), None)


async def select(app, pilot, key):
    options = app.screen.query_one("#choices", OptionList)
    keys = [options.get_option_at_index(i).id for i in range(options.option_count)]
    assert key in keys, f"missing {key!r} among {keys}"
    options.highlighted = keys.index(key)
    options.focus()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.parametrize("choice", PERMISSION_CHOICES, ids=lambda item: item.value)
async def test_settings_on_both_surfaces_persist_the_same_explicit_choices(
    settings, choice, tmp_path
):
    bot, chat = bot_for(settings), FakeChat()
    await bot.settings_command(chat.message_update("/settings"), None)
    await press(bot, chat, PERMISSION_TITLE)
    picker = chat.bot_messages[-1]
    assert choice.description in unescape(picker.text)
    await press(bot, chat, choice.label)
    assert await settings.read() == choice.value

    # A separately composed surface sees the saved value on entry.
    app = app_for(
        PermissionSettings(
            ConfigPermissionSetting(tmp_path / "config.toml"), profiles=settings.profiles
        )
    )
    async with app.run_test() as pilot:
        await app.show_settings()
        await pilot.pause()
        options = app.screen.query_one("#choices", OptionList)
        assert any(
            choice.label in str(options.get_option_at_index(i).prompt)
            for i in range(options.option_count)
        )
        await select(app, pilot, "settings:codex-permissions")
        # The current value is selected; Back/Escape never saves a different choice.
        options = app.screen.query_one("#choices", OptionList)
        assert options.get_option_at_index(options.highlighted).id == choice.value
        await pilot.press("escape")
        assert await settings.read() == choice.value
        await select(app, pilot, "settings:codex-permissions")
        other = next(item for item in PERMISSION_CHOICES if item.value != choice.value)
        await select(app, pilot, other.value)
        assert await settings.read() == other.value
        await select(app, pilot, "settings:codex-permissions")
        await select(app, pilot, choice.value)
        assert await settings.read() == choice.value
    reopened = await bot._settings_screen()
    assert choice.label in " ".join(item.text for row in reopened.keyboard for item in row)


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
@pytest.mark.parametrize("choice", PERMISSION_CHOICES, ids=lambda item: item.value)
async def test_both_flows_carry_only_the_deliberate_override(settings, choice, resume):
    sessions = Sessions()
    bot, chat = bot_for(settings, sessions), FakeChat()
    if resume:
        rendered = await bot._resume_catalogue_reply(f"{PROJECT.opaque_id}|codex|1")
    else:
        rendered = await bot._profiles_reply(PROJECT.opaque_id)
    # Use a real message and bound callback state for the optional picker.
    from remote_agents.adapters.telegram.service import _reply_arguments

    await chat.bot.send_message(chat_id=11, **_reply_arguments(rendered))
    bot.callbacks.bind_pending(11, chat.bot_messages[-1].message_id)
    await press(bot, chat, PERMISSION_TITLE)
    await press(bot, chat, choice.label)
    await press(bot, chat, "saved work" if resume else "Codex")
    assert len(sessions.commands) == 1
    assert sessions.commands[0].permissions == choice.value
    assert await settings.read() == PermissionPreset.APPROVE_FOR_ME

    sessions = Sessions()
    app = app_for(settings, sessions)
    async with app.run_test() as pilot:
        if resume:
            await app.push_screen(
                ResumeConversationsScreen(
                    PROJECT, "codex", ConversationCataloguePage((SUMMARY,), 1, 1)
                )
            )
        else:
            app.selection = LaunchSelection(project=PROJECT)
            await app.push_screen(ProfilesScreen())
        await pilot.pause()
        await select(app, pilot, "permissions")
        await select(app, pilot, choice.value)
        await select(app, pilot, str(SUMMARY.reference) if resume else "codex")
        assert len(sessions.commands) == 1
        assert sessions.commands[0].permissions == choice.value
        assert await settings.read() == PermissionPreset.APPROVE_FOR_ME


async def test_stale_setting_callback_is_once_only_and_invalid_choice_does_not_save(settings):
    bot, chat = bot_for(settings), FakeChat()
    await bot.settings_command(chat.message_update("/settings"), None)
    await press(bot, chat, PERMISSION_TITLE)
    message = chat.bot_messages[-1]
    token = button(message, "Ask me")
    await bot.callback(chat.press(token, on=message.message_id), None)
    await settings.set(PermissionPreset.PROVIDER_DEFAULT)
    await bot.callback(chat.press(token, on=message.message_id), None)
    assert await settings.read() == PermissionPreset.PROVIDER_DEFAULT
    result = await bot._reply_for("settings.permissions.set", "invented", token="bad", message_id=1)
    assert "permission" in result["text"].lower()
    assert await settings.read() == PermissionPreset.PROVIDER_DEFAULT


async def test_failed_save_is_visible_on_both_surfaces(settings):
    class Refusing:
        async def read(self):
            return PermissionPreset.APPROVE_FOR_ME

        async def write(self, value):
            raise OSError("fixture write refused")

    refusing = PermissionSettings(Refusing(), profiles=settings.profiles)
    bot, chat = bot_for(refusing), FakeChat()
    await bot.settings_command(chat.message_update("/settings"), None)
    await press(bot, chat, PERMISSION_TITLE)
    await press(bot, chat, "Ask me")
    assert "fixture write refused" in chat.bot_messages[-1].text
    app = app_for(refusing)
    async with app.run_test() as pilot:
        await app.show_settings()
        await pilot.pause()
        await select(app, pilot, "settings:codex-permissions")
        await select(app, pilot, "ask")
        assert any("fixture write refused" in item for item in announcements(app))


async def test_optional_picker_cancel_preserves_no_override(settings):
    bot_sessions = Sessions()
    bot, chat = bot_for(settings, bot_sessions), FakeChat()
    await bot.launch_command(chat.message_update("/launch"), None)
    await press(bot, chat, "fixture")
    await press(bot, chat, PERMISSION_TITLE)
    await press(bot, chat, "Cancel")
    await press(bot, chat, "Codex")
    assert bot_sessions.commands[0].permissions is None
    sessions = Sessions()
    app = app_for(settings, sessions)
    async with app.run_test() as pilot:
        app.selection = LaunchSelection(project=PROJECT)
        await app.push_screen(ProfilesScreen())
        await pilot.pause()
        await select(app, pilot, "permissions")
        await pilot.press("escape")
        await select(app, pilot, "codex")
        assert sessions.commands[0].permissions is None


async def test_session_inspection_names_the_snapshot_instead_of_the_new_default(settings):
    class Existing(Sessions):
        async def list_sessions(self):
            return (self.record,)

    sessions = Existing()
    screen = await bot_for(settings, sessions)._detail_reply(str(sessions.record.session_id))
    assert "Launch permissions" in screen.text and "Ask me" in screen.text
    app = app_for(settings, sessions)
    async with app.run_test() as pilot:
        await app.show_detail(str(sessions.record.session_id))
        await pilot.pause()
        from tui_feedback import status

        assert "Launch permissions" in status(app) and "Ask me" in status(app)


async def test_stale_override_for_an_unsupported_agent_is_refused_before_launch(settings):
    sessions = Sessions()
    bot = bot_for(settings, sessions)
    bot.profiles = (ProfileAvailability("claude", True),)
    token = bot._callback(
        "launch.profile", f"pp1:full_access:{PROJECT.opaque_id}|claude", mutation=True
    )
    bot.callbacks.bind_pending(11, 1)
    result = await bot._reply_for(
        "launch.profile", f"pp1:full_access:{PROJECT.opaque_id}|claude", token=token, message_id=1
    )
    assert "permission" in result["text"].lower() and "unavailable" in result["text"].lower()
    assert sessions.commands == []


async def test_open_surfaces_refresh_after_another_process_saves(settings, tmp_path):
    import asyncio
    import sys

    bot = bot_for(settings)
    await bot._settings_screen()
    app = app_for(settings)
    async with app.run_test() as pilot:
        await app.show_settings()
        await pilot.pause()
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from pathlib import Path; from remote_agents.config import write_config_key; "
            "import sys; write_config_key(Path(sys.argv[1]), "
            "'codex', 'default_permissions', 'ask')",
            str(tmp_path / "config.toml"),
        )
        assert await child.wait() == 0
        await app.screen.refresh_contents()
        options = app.screen.query_one("#choices", OptionList)
        assert any(
            "Ask me" in str(options.get_option_at_index(i).prompt)
            for i in range(options.option_count)
        )
        screen = await bot._settings_screen()
        assert any("Ask me" in item.text for row in screen.keyboard for item in row)


@pytest.mark.parametrize(
    "action,entity",
    [
        ("launch.project", PROJECT.opaque_id),
        ("launch.profile", f"{PROJECT.opaque_id}|codex"),
        ("resume.page", f"{PROJECT.opaque_id}|codex|1"),
        ("resume.confirm", str(SUMMARY.reference)),
    ],
)
async def test_stale_explicit_choice_is_refused_when_capability_disappears(
    settings, action, entity
):
    sessions = Sessions()
    bot = bot_for(settings, sessions)
    bot.backend = replace(bot.backend, permission_presets=None)
    encoded = f"pp1:ask:{entity}"
    result = await bot._reply_for(action, encoded, token="stale", message_id=1)
    assert "permissions are unavailable" in result["text"]
    assert sessions.commands == []
