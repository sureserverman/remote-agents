"""Live Telegram service composition is owner-only and CLI-addressable without a network."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from stop_results import a_clean_stop, a_stop_that_did_not_take
from telegram.error import BadRequest

from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.telegram.service import (
    _BOT_DESCRIPTION,
    _BOT_SHORT_DESCRIPTION,
    _OWNER_COMMANDS,
    PrivateBotBoundary,
    _sync_owner_metadata,
    audit_bot_metadata,
)
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import GRACEFUL_TIMEOUT, UNKNOWN_SESSION
from remote_agents.bootstrap import ServiceComposition, _resolve_profile_executable, main
from remote_agents.config import TelegramSecrets
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalTargetMissing


def test_private_bot_boundary_accepts_only_the_exact_configured_private_chat() -> None:
    boundary = PrivateBotBoundary(7, 11)
    trusted = SimpleNamespace(
        effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=11, type="private")
    )
    foreign_user = SimpleNamespace(
        effective_user=SimpleNamespace(id=8), effective_chat=SimpleNamespace(id=11, type="private")
    )
    group = SimpleNamespace(
        effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=11, type="group")
    )

    assert boundary.permits(trusted)
    assert not boundary.permits(foreign_user)
    assert not boundary.permits(group)


def test_profile_executable_resolver_includes_the_user_nvm_installation(tmp_path) -> None:
    executable = tmp_path / ".nvm" / "versions" / "node" / "v22" / "bin" / "codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    assert _resolve_profile_executable("codex", tmp_path) == executable


def test_doctor_uses_the_private_default_config_and_reports_operational_components(
    tmp_path, monkeypatch, capsys
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n",
        encoding="utf-8",
    )
    paths = _DoctorPaths(config)
    monkeypatch.setattr("remote_agents.bootstrap.ProductionPaths.for_home", lambda _home: paths)
    monkeypatch.setattr("remote_agents.bootstrap.database_is_ready", lambda _path: True)
    monkeypatch.setattr("remote_agents.bootstrap._command_succeeds", lambda _argv: True)
    monkeypatch.setattr(
        "remote_agents.bootstrap._telegram_credentials_are_private", lambda _paths: True
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.load_registry",
        lambda _path: SimpleNamespace(projects=(), error=None),
    )
    monkeypatch.setattr("remote_agents.bootstrap.discover_projects", lambda _path: ())
    monkeypatch.setattr("remote_agents.bootstrap.build_catalogue", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        "remote_agents.bootstrap.probe_profiles",
        lambda *_args, **_kwargs: (
            _compatibility("claude"),
            _compatibility("claude-remote"),
            _compatibility("codex"),
            _compatibility("opencode"),
            _compatibility("cursor-agent"),
        ),
    )

    assert main(["doctor", "--json"]) == 0

    report = __import__("json").loads(capsys.readouterr().out)
    assert report["healthy"] is True
    assert set(report["components"]) == {"core", "store", "tmux", "telegram", "service", "profiles"}
    assert [profile["status"] for profile in report["profiles"]] == ["AVAILABLE"] * 5


@pytest.mark.asyncio
async def test_private_bot_boundary_renders_and_refreshes_only_issued_owner_callbacks() -> None:
    boundary = PrivateBotBoundary(7, 11)
    message = _Message()
    update = _trusted_update(message=message)

    await boundary.start(update, None)

    assert (
        message.replies[0]["text"]
        == "<b>Remote agents</b>\nActive: 0 · Preserved: 0\nChoose an action."
    )
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    callback = _Callback(launch)
    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None]
    assert callback.edits[0]["text"] == "<b>Projects 1/1</b>\nSelect a project to launch."

    await boundary.callback(_trusted_update(callback=callback), None)

    # The first press replaced this message's keyboard, which pruned the token it came from
    # -- so the second press of the same button is a race, answered and redrawn.
    assert callback.answers == [None, "That screen has moved on."]
    assert callback.edits[1]["text"].startswith("<b>Remote agents</b>")


@pytest.mark.asyncio
async def test_private_bot_boundary_submits_only_a_confirmed_opaque_launch() -> None:
    launcher = _Launcher()
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        profiles=(ProfileAvailability("claude", True),),
        launcher=launcher,
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    projects = _Callback(launch)
    await boundary.callback(_trusted_update(callback=projects), None)
    project = projects.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    profiles = _Callback(project)
    await boundary.callback(_trusted_update(callback=profiles), None)
    profile = profiles.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    confirmation = _Callback(profile)
    await boundary.callback(_trusted_update(callback=confirmation), None)
    confirm = confirmation.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    submitted = _Callback(confirm)
    await boundary.callback(_trusted_update(callback=submitted), None)

    assert len(launcher.commands) == 1
    assert str(launcher.commands[0].project_id) == "a" * 24
    assert str(launcher.commands[0].profile_id) == "claude"


@pytest.mark.asyncio
async def test_failed_launch_explains_that_workspace_trust_is_never_approved_remotely() -> None:
    failed = _record(SessionState.FAILED, "failed", ProjectId("a" * 24))
    launcher = _Launcher()
    launcher.launch_result = failed
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        profiles=(ProfileAvailability("cursor-agent", True),),
        launcher=launcher,
    )
    token = boundary.callbacks.create(
        "launch.confirm", "a" * 24 + "|cursor-agent", 7, 11, 1, mutation=True
    )

    reply = await boundary._launch_reply("a" * 24 + "|cursor-agent", token, 1)

    assert "Session did not become ready" in reply["text"]
    assert "never approved remotely" in reply["text"]
    assert [button.text for row in reply["reply_markup"].inline_keyboard for button in row] == [
        "Details",
        "Sessions",
        "Launch another",
        "Home",
    ]


@pytest.mark.asyncio
async def test_private_bot_boundary_ignores_a_duplicate_telegram_edit() -> None:
    boundary = PrivateBotBoundary(7, 11)
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    callback = _Callback(launch, edit_error=BadRequest("Message is not modified"))

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None]


@pytest.mark.asyncio
async def test_private_bot_boundary_hides_ended_history_from_sessions_list() -> None:
    launcher = _Launcher()
    launcher.records = [
        _record(SessionState.RUNNING, "active", ProjectId("a" * 24)),
        _record(SessionState.ENDED, "ended", ProjectId("a" * 24)),
    ]
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=launcher,
    )

    reply = await boundary._sessions_reply()

    labels = tuple(button.text for row in reply.keyboard for button in row)
    assert labels[0].startswith("Demo · codex · regular · #1 · active · running · ")
    assert labels[1:] == ("Refresh", "Home")
    assert "ended" not in labels


@pytest.mark.asyncio
async def test_private_bot_boundary_searches_projects_and_labels_a_launch() -> None:
    launcher = _Launcher()
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(
            CatalogProject("a" * 24, "opaque-editor", "writing", "Registered"),
            CatalogProject("b" * 24, "opaque-verse", "writing", "Registered"),
        ),
        profiles=(ProfileAvailability("codex", True),),
        launcher=launcher,
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    projects = _Callback(launch)
    await boundary.callback(_trusted_update(callback=projects), None)
    search = projects.edits[0]["reply_markup"].inline_keyboard[2][0].callback_data
    awaiting_search = _Callback(search)
    await boundary.callback(_trusted_update(callback=awaiting_search), None)
    assert awaiting_search.edits[0]["text"] == "Reply below with a project name."
    assert (
        awaiting_search.message.replies[0]["reply_markup"].input_field_placeholder == "Project name"
    )

    result = _Message("verse")
    await boundary.text(_trusted_update(message=result), None)

    project = result.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    profiles = _Callback(project)
    await boundary.callback(_trusted_update(callback=profiles), None)
    profile = profiles.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    confirmation = _Callback(profile)
    await boundary.callback(_trusted_update(callback=confirmation), None)
    label = confirmation.edits[0]["reply_markup"].inline_keyboard[1][0].callback_data
    awaiting_label = _Callback(label)
    await boundary.callback(_trusted_update(callback=awaiting_label), None)
    assert awaiting_label.edits[0]["text"] == "Reply below with an optional session label."
    assert (
        awaiting_label.message.replies[0]["reply_markup"].input_field_placeholder
        == "Optional session label"
    )

    labelled = _Message("  review  ")
    await boundary.text(_trusted_update(message=labelled), None)
    confirm = labelled.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    submitted = _Callback(confirm)
    await boundary.callback(_trusted_update(callback=submitted), None)

    assert str(launcher.commands[0].project_id) == "b" * 24
    assert launcher.commands[0].label == "review"


@pytest.mark.asyncio
async def test_owner_commands_render_only_the_private_chat_surface() -> None:
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
    )
    launch = _Message()
    sessions = _Message()
    help_message = _Message()

    await boundary.launch_command(_trusted_update(message=launch), None)
    await boundary.sessions_command(_trusted_update(message=sessions), None)
    await boundary.help_command(_trusted_update(message=help_message), None)

    assert launch.replies[0]["text"] == "<b>Projects 1/1</b>\nSelect a project to launch."
    assert sessions.replies[0]["text"] == "<b>Sessions</b>\nNothing is running."
    # The empty list offers the action that fills it rather than a disabled-looking row.
    assert sessions.replies[0]["reply_markup"].inline_keyboard[0][0].text == "Launch"
    # Help is a screen like any other now: it carries a keyboard and names the real actions.
    assert help_message.replies[0]["text"].startswith("<b>Remote agents</b>")
    assert "Stop and close" in help_message.replies[0]["text"]
    assert help_message.replies[0]["reply_markup"].inline_keyboard[-1][-1].text == "Home"


@pytest.mark.asyncio
async def test_inspection_sends_the_existing_oversized_output_as_a_utf8_attachment() -> None:
    session = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))

    async def capture(_session_id):
        return "x" * 5000

    launcher = _Launcher()
    launcher.records = [session]
    boundary = PrivateBotBoundary(7, 11, launcher=launcher, capture=capture)
    await boundary.start(_trusted_update(message=_Message()), None)
    detail = await boundary._detail_reply(str(session.session_id), 1)
    boundary.callbacks.bind_pending(11, 1)
    inspect = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if button.text == "Inspect"
    )
    callback = _Callback(inspect)

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.edits[0]["text"] == "<pre>Output is attached as UTF-8 text.</pre>"
    # Marked unforwardable: a pane's transcript is exactly the thing that should not be one
    # tap from leaving this private chat.
    assert callback.message.documents == [
        {"document": b"x" * 5000, "filename": "session-output.txt", "protect_content": True}
    ]


@pytest.mark.asyncio
async def test_inspecting_a_pane_that_died_since_the_view_was_drawn_answers_the_press() -> None:
    """A session killed between drawing and pressing must not raise into the handler.

    This is the OOM-kill case: the record still says RUNNING because reconciliation has
    not run yet, so Inspect is on screen for a pane that no longer exists.
    """
    session = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))

    async def capture(session_id):
        raise TerminalTargetMissing(f"managed target is gone: ra-{session_id}")

    launcher = _Launcher()
    launcher.records = [session]
    boundary = PrivateBotBoundary(7, 11, launcher=launcher, capture=capture)
    await boundary.start(_trusted_update(message=_Message()), None)
    detail = await boundary._detail_reply(str(session.session_id), 1)
    boundary.callbacks.bind_pending(11, 1)
    inspect = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if button.text == "Inspect"
    )
    callback = _Callback(inspect)

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.edits[0]["text"] == "Inspection is unavailable."
    assert callback.message.documents == []


@pytest.mark.asyncio
async def test_owner_metadata_is_private_and_matches_reviewed_values() -> None:
    bot = _MetadataBot()

    await _sync_owner_metadata(bot, 11)
    report = await audit_bot_metadata(bot, 11)

    assert bot.default_commands_deleted is True
    assert report == {
        "default_commands": [],
        "owner_commands": [command.command for command in _OWNER_COMMANDS],
        "owner_menu": "commands",
        "description_matches": True,
        "short_description_matches": True,
        "healthy": True,
    }
    assert bot.description == _BOT_DESCRIPTION
    assert bot.short_description == _BOT_SHORT_DESCRIPTION


@pytest.mark.asyncio
async def test_private_bot_boundary_pages_through_the_entire_project_catalogue() -> None:
    catalogue = tuple(
        CatalogProject(f"{number:024d}", f"Project {number}", "tests", "Registered")
        for number in range(25)
    )
    boundary = PrivateBotBoundary(7, 11, catalogue=catalogue, project_page_size=10)
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    first = _Callback(launch)
    await boundary.callback(_trusted_update(callback=first), None)

    first_page = first.edits[0]["reply_markup"].inline_keyboard
    assert first.edits[0]["text"] == "<b>Projects 1/3</b>\nSelect a project to launch."
    assert [row[0].text for row in first_page[:10]] == [f"Project {number}" for number in range(10)]
    second = _Callback(
        next(button.callback_data for row in first_page for button in row if button.text == "Next")
    )
    await boundary.callback(_trusted_update(callback=second), None)

    second_page = second.edits[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in second_page[:10]] == [
        f"Project {number}" for number in range(10, 20)
    ]
    third = _Callback(
        next(button.callback_data for row in second_page for button in row if button.text == "Next")
    )
    await boundary.callback(_trusted_update(callback=third), None)

    third_page = third.edits[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in third_page[:5]] == [
        f"Project {number}" for number in range(20, 25)
    ]


class _SilentTerminal:
    """The serve wiring reconciles before polling; this test cares only about the poll."""

    async def managed_observations(self) -> tuple[object, ...]:
        return ()


class _SilentReconciler:
    async def reconcile(self, observations: tuple[object, ...]) -> tuple[object, ...]:
        return ()


def test_serve_command_loads_config_and_runs_the_injected_private_bot(
    tmp_path, monkeypatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n",
        encoding="utf-8",
    )
    received: list[TelegramSecrets] = []

    async def serve(secrets: TelegramSecrets, _boundary: PrivateBotBoundary) -> None:
        received.append(secrets)

    monkeypatch.setattr(
        "remote_agents.bootstrap.load_secrets", lambda: TelegramSecrets("token", 7, 11)
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home",
        lambda _home: _Paths(tmp_path / "sessions.sqlite3"),
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap._private_boundary",
        lambda _config, _connection, _paths: ServiceComposition(
            PrivateBotBoundary(7, 11), _SilentTerminal(), _SilentReconciler()
        ),
    )

    assert main(["serve", "--config", str(config)], serve_runner=serve) == 0
    assert received == [TelegramSecrets("token", 7, 11)]


def test_telegram_ui_audit_reads_only_the_private_environment_file(
    tmp_path, monkeypatch, capsys
) -> None:
    environment = tmp_path / "telegram.env"
    environment.write_text(
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=token\n"
        "REMOTE_AGENTS_OWNER_USER_ID=7\n"
        "REMOTE_AGENTS_OWNER_CHAT_ID=11\n",
        encoding="utf-8",
    )
    environment.chmod(0o600)
    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home", lambda _home: _AuditPaths(environment)
    )

    async def audit(secrets):
        assert secrets == TelegramSecrets("token", 7, 11)
        return {"healthy": True, "default_commands": []}

    monkeypatch.setattr("remote_agents.bootstrap.audit_owner_metadata", audit)

    assert main(["telegram-ui-audit", "--json"]) == 0
    assert __import__("json").loads(capsys.readouterr().out) == {
        "default_commands": [],
        "healthy": True,
    }


class _Paths:
    def __init__(self, database_path) -> None:
        self.database_path = database_path

    def ensure_directories(self) -> None:
        return None

    def require_private_environment(self):
        return None

    def open_database(self, *_args, **_kwargs):
        return _Connection()


class _DoctorPaths:
    def __init__(self, config_path) -> None:
        self.config_path = config_path
        self.home = config_path.parent


class _AuditPaths:
    def __init__(self, environment_path) -> None:
        self.environment_path = environment_path

    def require_private_environment(self):
        return self.environment_path


def _compatibility(profile_id: str):
    from remote_agents.domain.profiles import ProfileCompatibility

    return ProfileCompatibility(ProfileId(profile_id), True, "1.2.3", "AVAILABLE", None)


class _Connection:
    def close(self) -> None:
        return None


class _Launcher:
    def __init__(self) -> None:
        self.commands = []
        self.records = []
        self.launch_result = None
        self.stopped: list[str] = []
        self.leave_running = False
        #: Which cause `leave_running` models. Two of them, and they are the whole of BL-008
        #: on this surface — the bot could see the session was still there and could not see
        #: which of two unrelated things had gone wrong.
        self.graceful_detail = GRACEFUL_TIMEOUT

    async def launch(self, command):
        self.commands.append(command)
        return self.launch_result

    async def list_sessions(self):
        return self.records

    async def refresh_readiness(self) -> None:
        return None

    async def graceful_stop(self, command):
        """Model a stop that ends the session, or one whose agent never exits.

        `leave_running` is the graceful-stop timeout: the service restores RUNNING and
        removes nothing, which is the outcome the bot most needs to report honestly.
        """
        self.stopped.append("graceful")
        if not self.leave_running:
            self.records = [
                record for record in self.records if record.session_id != command.session_id
            ]
            return a_clean_stop(command.session_id)
        # The failure, reported the way the real runtime reports it. It used to answer `None`,
        # which the surfaces discarded — so this double modelled the *records* faithfully and
        # the *observation* not at all, which is exactly the half BL-008 was about.
        return a_stop_that_did_not_take(self.graceful_detail, command.session_id)

    async def force_stop(self, command):
        self.stopped.append("force")
        self.records = [
            record for record in self.records if record.session_id != command.session_id
        ]
        return None


class _Message:
    """One chat message, with the id the boundary binds its keyboard to.

    `reply_text` answers with a message rather than None because the boundary now uses what
    Telegram returns: a keyboard is minted before it is sent, so the send is what tells the
    store which message carries it. The doubles share one id, which is the shape this chat
    actually has — one live view.
    """

    def __init__(self, text: str | None = None, message_id: int = 1) -> None:
        self.replies: list[dict[str, object]] = []
        self.documents: list[dict[str, object]] = []
        self.text = text
        self.message_id = message_id

    async def reply_text(self, text: str | None = None, **kwargs: object) -> _Message:
        if text is not None:
            kwargs["text"] = text
        self.replies.append(kwargs)
        return self

    async def reply_document(self, **kwargs: object) -> None:
        document = kwargs["document"]
        self.documents.append(
            {
                "document": document.read(),
                "filename": kwargs["filename"],
                "protect_content": kwargs.get("protect_content", False),
            }
        )


class _Callback:
    def __init__(self, data: str, *, edit_error: Exception | None = None) -> None:
        self.data = data
        self.edit_error = edit_error
        self.answers: list[str | None] = []
        self.alerts: list[bool] = []
        self.edits: list[dict[str, object]] = []
        self.message = _Message()

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append(text)
        self.alerts.append(show_alert)

    async def edit_message_text(self, **kwargs: object) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)


class _MetadataBot:
    def __init__(self) -> None:
        self.default_commands_deleted = False
        self.owner_commands = []
        self.owner_menu = None
        self.description = ""
        self.short_description = ""

    async def delete_my_commands(self) -> None:
        self.default_commands_deleted = True

    async def set_my_commands(self, commands, *, scope) -> None:
        self.owner_commands = list(commands)
        self.owner_scope = scope

    async def set_chat_menu_button(self, *, chat_id, menu_button) -> None:
        self.owner_menu = (chat_id, menu_button)

    async def set_my_description(self, description) -> None:
        self.description = description

    async def set_my_short_description(self, short_description) -> None:
        self.short_description = short_description

    async def get_my_commands(self, *, scope=None):
        return self.owner_commands if scope is not None else []

    async def get_chat_menu_button(self, *, chat_id):
        assert chat_id == self.owner_menu[0]
        return self.owner_menu[1]

    async def get_my_description(self):
        return SimpleNamespace(description=self.description)

    async def get_my_short_description(self):
        return SimpleNamespace(short_description=self.short_description)


def _catalogue(count: int) -> tuple[CatalogProject, ...]:
    return tuple(
        CatalogProject(f"{index:024d}", f"project-{index}", "tests", "Registered")
        for index in range(count)
    )


def test_resume_picks_a_project_the_same_way_launch_does() -> None:
    """Resume rendered the whole catalogue as one button per project, unpaged.

    At 94 projects that was 95 keyboard rows against launch's 14, and Telegram refuses a
    keyboard past 100 buttons, so the screen was a few projects away from not rendering.
    Both flows now share one renderer; only the action each button carries differs.
    """
    boundary = PrivateBotBoundary(7, 11, catalogue=_catalogue(94))

    resume = boundary._resume_projects_reply()
    launch = boundary._projects_reply(boundary.catalogue, view_id="all")

    assert len(resume.keyboard) == len(launch.keyboard)
    assert resume.text.startswith("<b>Resume 1/10</b>")
    assert [[button.text for button in row] for row in resume.keyboard[-3:]] == [
        ["Next"],
        ["Search"],
        ["Back", "Home"],
    ]


def test_a_resume_project_page_stays_inside_the_resume_flow() -> None:
    """A project chosen after paging or searching must still resume, never launch."""
    boundary = PrivateBotBoundary(7, 11, catalogue=_catalogue(30))
    boundary._resume_projects_reply()

    second = boundary._project_page_reply("all|2", flow="resume")

    def _action(token: str) -> str | None:
        boundary.callbacks.bind_pending(11, 1)
        state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=1)
        return None if state is None else state.action

    assert second.text.startswith("<b>Resume 2/3</b>")
    assert _action(second.keyboard[0][0].callback_data) == "resume.project"
    assert _action(second.keyboard[-2][0].callback_data) == "resume.search"


def test_the_two_flows_cannot_page_into_each_others_stored_views() -> None:
    """Launch and resume both store a view called "all"; keying by flow keeps them apart."""
    boundary = PrivateBotBoundary(7, 11, catalogue=_catalogue(30))
    boundary._projects_reply(_catalogue(30), view_id="search", flow="launch")

    # Resume never stored a "search" view, so paging into one is refused rather than
    # silently answered with the launch flow's results. Only a search is refused: an "all"
    # view is reconstructible from the catalogue and re-renders instead.
    assert boundary._project_page_reply("search|2", flow="resume").text == (
        "That search is no longer open. Search again."
    )
    assert boundary._project_page_reply("all|2", flow="resume").text.startswith("<b>Resume 2/3</b>")


def _stop_boundary(*records: SessionRecord) -> tuple[PrivateBotBoundary, _Launcher]:
    """A boundary holding `records`, ready to render and be pressed."""
    launcher = _Launcher()
    launcher.records = list(records)
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=launcher,
    )
    return boundary, launcher


@pytest.mark.asyncio
async def test_session_detail_offers_a_way_back_and_keeps_the_stops_on_their_own_row() -> None:
    """Shape is the only separator Telegram gives, so the stops must not look like reads."""
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, _ = _stop_boundary(running)

    detail = await boundary._detail_reply(str(running.session_id))

    rows = [[button.text for button in row] for row in detail.keyboard]
    assert rows[0] == ["Inspect"]
    assert rows[-2] == ["Stop and close", "Force stop"]
    assert rows[-1] == ["Back", "Home"]


@pytest.mark.asyncio
async def test_refresh_is_reachable_from_the_two_screens_whose_answer_goes_stale() -> None:
    """`nav.refresh` had a live handler and no button anywhere that could reach it."""
    boundary, _ = _stop_boundary(_record(SessionState.RUNNING, "active", ProjectId("a" * 24)))

    home = await boundary._home_reply()
    sessions = await boundary._sessions_reply()

    def _resolved_action(token: str) -> str | None:
        boundary.callbacks.bind_pending(11, 1)
        state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=1)
        return None if state is None else state.action

    home_refresh = next(
        button.callback_data
        for row in home["reply_markup"].inline_keyboard
        for button in row
        if button.text == "Refresh"
    )
    sessions_refresh = next(
        button.callback_data
        for row in sessions.keyboard
        for button in row
        if button.text == "Refresh"
    )
    assert _resolved_action(home_refresh) == "nav.refresh"
    # Sessions refreshes itself rather than bouncing the owner to the dashboard.
    assert _resolved_action(sessions_refresh) == "sessions.page"


@pytest.mark.asyncio
async def test_the_sessions_list_pages_instead_of_growing_past_the_message() -> None:
    records = [
        _record(SessionState.RUNNING, f"active-{index}", ProjectId("a" * 24)) for index in range(9)
    ]
    boundary, _ = _stop_boundary(*records)
    boundary.session_page_size = 4

    first = await boundary._sessions_reply()
    last = await boundary._sessions_reply(3)
    beyond = await boundary._sessions_reply(99)

    assert first.text == "<b>Sessions 1/3</b>"
    assert [button.text for button in first.keyboard[-2]] == ["Next"]
    assert len(first.keyboard) == 4 + 2
    assert last.text == "<b>Sessions 3/3</b>"
    assert [button.text for button in last.keyboard[-2]] == ["Previous"]
    # A page number past the end clamps rather than rendering an empty list.
    assert beyond.text == last.text


@pytest.mark.asyncio
async def test_force_confirmation_names_the_session_and_puts_cancel_before_the_kill() -> None:
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, _ = _stop_boundary(running)
    # The name the bot shows carries the catalogue's project name, not the opaque slug.
    subject = (await boundary._record(str(running.session_id))).display.rendered
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, "force", 7, 11
    )
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("force", token, 1)

    rows = [[button.text for button in row] for row in reply["reply_markup"].inline_keyboard]
    assert subject in reply["text"]
    assert "cannot be undone" in reply["text"]
    assert rows[0] == ["Cancel"]
    assert rows[1] == ["Force stop"]


@pytest.mark.asyncio
async def test_a_completed_stop_names_the_session_and_what_became_of_its_output() -> None:
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, launcher = _stop_boundary(running)
    subject = (await boundary._record(str(running.session_id))).display.rendered
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, "graceful", 7, 11
    )
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("graceful", token, 1)

    assert launcher.stopped == ["graceful"]
    assert subject in reply["text"]
    assert "The session has ended" in reply["text"]
    assert "no longer there to inspect" in reply["text"]


@pytest.mark.asyncio
async def test_a_graceful_stop_that_times_out_reports_the_session_as_still_running() -> None:
    """The one outcome the owner has to act on, and the one "completed" used to hide."""
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, launcher = _stop_boundary(running)
    launcher.leave_running = True
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, "graceful", 7, 11
    )
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("graceful", token, 1)

    assert "is still running" in reply["text"]
    assert "did not exit in time" in reply["text"]
    assert [button.text for row in reply["reply_markup"].inline_keyboard for button in row] == [
        "Open session",
        "Back",
        "Home",
    ]


def test_only_the_actions_that_make_the_owner_wait_get_a_pending_notice() -> None:
    boundary = PrivateBotBoundary(7, 11)

    assert boundary._pending_notice("graceful") is not None
    assert boundary._pending_notice("launch.confirm") is not None
    assert boundary._pending_notice("session.detail") is None
    # A first press on force only opens the confirmation, so nothing is running yet — and
    # that is now readable from the action alone, with no confirmation state to consult.
    assert boundary._pending_notice("force") is None
    assert boundary._pending_notice(CONFIRMED_FORCE) is not None


@pytest.mark.asyncio
async def test_a_slow_action_shows_a_keyboardless_pending_screen_before_its_result() -> None:
    """Telegram clears the spinner the moment the query is answered, which must be at once."""
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, _ = _stop_boundary(running)
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, "graceful", 7, 11
    )
    callback = _Callback(token)
    boundary.callbacks.bind_pending(11, callback.message.message_id)

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == ["Stopping the session — waiting for the agent to exit…"]
    assert len(callback.edits) == 2
    assert callback.edits[0]["text"] == "Stopping the session — waiting for the agent to exit…"
    # No keyboard while it runs: the button that started this cannot be pressed again.
    assert callback.edits[0]["reply_markup"].inline_keyboard == ()
    assert "The session has ended" in callback.edits[1]["text"]


@pytest.mark.asyncio
async def test_a_press_this_screen_cannot_account_for_is_a_race_not_an_error() -> None:
    """Nothing expires any more, so the modal alert that said so is gone with it.

    A token only fails to resolve when the keyboard that drew it has already been replaced
    on this message — a thumb racing a redraw. That earns a toast and a redraw, not a modal
    telling the owner their view died.
    """
    boundary, _ = _stop_boundary()
    callback = _Callback("c1_never_issued")

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == ["That screen has moved on."]
    assert callback.alerts == [False]
    assert "Remote agents" in str(callback.edits[0]["text"])


def _trusted_update(*, message: _Message | None = None, callback: _Callback | None = None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=11, type="private"),
        effective_message=message,
        callback_query=callback,
    )


def _record(state: SessionState, label: str, project_id: ProjectId) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        project_id,
        ProfileId("codex"),
        SessionDisplayIdentity("demo", "codex", "regular", 1, label),
        state,
        datetime.now(UTC),
    )


# BL-008 on this surface — which of the two causes, not just that the session is still there --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail,names,denies",
    [
        (UNKNOWN_SESSION, "The stop was never sent.", "did not exit in time"),
        (GRACEFUL_TIMEOUT, "The agent did not exit in time.", "never sent"),
    ],
)
async def test_the_bot_names_which_cause_left_the_session_running(
    detail: str, names: str, denies: str
) -> None:
    """The bot used to infer the cause from the session still being listed, and got it wrong.

    Finding the session still there says a stop did not take effect; it cannot say why,
    because both causes leave identical evidence. So "It did not exit in time" was asserted
    for `unknown_session` too — where no exit sequence was ever sent — and an owner who
    believed it would sit waiting for an agent nobody had asked to stop.

    `denies` is the half that makes this a real pair: each case asserts the *other* cause's
    wording is absent, so wording that converges back into one sentence fails here rather than
    passing both cases on the same string.
    """
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, launcher = _stop_boundary(running)
    launcher.leave_running = True
    launcher.graceful_detail = detail
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, "graceful", 7, 11
    )
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("graceful", token, 1)

    assert names in reply["text"], reply["text"]
    assert denies not in reply["text"], reply["text"]


# The render pipeline itself -- the gap that hid two dead-button defects ------------------


def _press(callback: _Callback) -> _Callback:
    """The next press, on the same message the last render edited."""
    return callback


@pytest.mark.asyncio
async def test_a_stop_button_survives_the_render_that_drew_it() -> None:
    """Drive a *mutating* action through `callback()`, which no stop test used to do.

    Every other stop test calls `_detail_reply`/`_stop_reply` directly, so none of them ran
    the edit-prune-bind pipeline — and a review found that stop tokens were minted already
    bound to the message being redrawn, so the prune step destroyed them in the very pass
    that drew them. Every stop button in the live bot was dead, and the whole suite was green.
    This presses one, the way a phone does.
    """
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, launcher = _stop_boundary(running)
    await boundary.start(_trusted_update(message=_Message()), None)
    sessions = _Callback(_button(await boundary._home_reply(), "Sessions"))
    boundary.callbacks.bind_pending(11, sessions.message.message_id)

    await boundary.callback(_trusted_update(callback=sessions), None)
    row = _Callback(_edited_button(sessions, 0))
    await boundary.callback(_trusted_update(callback=row), None)
    graceful = _Callback(_edited_button(row, -1, text="Stop and close"))
    await boundary.callback(_trusted_update(callback=graceful), None)

    assert graceful.answers == ["Stopping the session — waiting for the agent to exit…"], (
        "the stop button did not resolve, so the press did nothing"
    )
    assert launcher.stopped == ["graceful"], "the stop never reached the application"


@pytest.mark.asyncio
async def test_the_force_confirmation_button_survives_the_render_that_drew_it() -> None:
    """The second press must reach the kill; the confirmation screen is a re-render."""
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, launcher = _stop_boundary(running)
    await boundary.start(_trusted_update(message=_Message()), None)
    sessions = _Callback(_button(await boundary._home_reply(), "Sessions"))
    boundary.callbacks.bind_pending(11, sessions.message.message_id)

    await boundary.callback(_trusted_update(callback=sessions), None)
    row = _Callback(_edited_button(sessions, 0))
    await boundary.callback(_trusted_update(callback=row), None)
    force = _Callback(_edited_button(row, -1, text="Force stop"))
    await boundary.callback(_trusted_update(callback=force), None)

    assert "Force stop" in str(force.edits[0]["text"]) and "cannot be undone" in str(
        force.edits[0]["text"]
    )
    confirmed = _Callback(_edited_button(force, 0, text="Force stop"))
    await boundary.callback(_trusted_update(callback=confirmed), None)

    assert confirmed.answers == ["Force stopping the session…"]
    assert launcher.stopped == ["force"], "the confirmed force never reached the application"


def _button(reply: dict[str, object], text: str) -> str:
    return next(
        button.callback_data
        for row in reply["reply_markup"].inline_keyboard
        for button in row
        if button.text == text
    )


def _edited_button(callback: _Callback, index: int, *, text: str | None = None) -> str:
    keyboard = callback.edits[-1]["reply_markup"].inline_keyboard
    if text is not None:
        return next(
            button.callback_data for row in keyboard for button in row if button.text == text
        )
    return keyboard[index][0].callback_data


@pytest.mark.asyncio
async def test_a_button_drawn_before_a_restart_still_works_after_one(tmp_path) -> None:
    """The reported defect, end to end: a new composition over the same database.

    The first connection is **closed** before the second is opened, which is what
    `bootstrap.main()` actually does across a restart — sharing one handle would prove only
    that two objects can read one open file, and would survive a store that never persisted
    anything at all.
    """
    database = tmp_path / "sessions.sqlite3"
    connection = open_database(database)
    before = PrivateBotBoundary(7, 11, callbacks=SQLiteCallbackStateStore(connection))
    message = _Message()
    await before.start(_trusted_update(message=message), None)
    sessions = _button(message.replies[0], "Sessions")
    connection.close()

    after = PrivateBotBoundary(7, 11, callbacks=SQLiteCallbackStateStore(open_database(database)))
    callback = _Callback(sessions)
    await after.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None], "the restarted service refused a button it had drawn"
    assert "Sessions" in str(callback.edits[0]["text"])
