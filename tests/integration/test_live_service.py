"""Live Telegram service composition is owner-only and CLI-addressable without a network."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from backends import SessionUseCaseDouble, backend_for
from stop_results import a_clean_stop, a_stop_that_did_not_take, a_verified_force_stop
from telegram.error import BadRequest, TelegramError

from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import (
    _BOT_DESCRIPTION,
    _BOT_SHORT_DESCRIPTION,
    _OWNER_COMMANDS,
    PrivateBotBoundary,
    _reply_arguments,
    _sync_owner_metadata,
    audit_bot_metadata,
    build_private_bot,
)
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import GRACEFUL_TIMEOUT, UNKNOWN_SESSION
from remote_agents.bootstrap import (
    _resolve_profile_executable,
    main,
)
from remote_agents.composition.service import ServiceComposition, _watch_activity_once
from remote_agents.config import ConfigError, TelegramSecrets
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
)
from remote_agents.ports.session_store import ProjectUsage
from remote_agents.ports.terminal import TerminalTargetMissing


def test_private_bot_boundary_accepts_only_the_exact_configured_private_chat() -> None:
    boundary = build_private_bot(7, 11)
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
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
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
    monkeypatch.setattr(
        "remote_agents.composition.backend.load_registry",
        lambda _path: SimpleNamespace(projects=(), error=None),
    )
    monkeypatch.setattr("remote_agents.bootstrap.discover_projects", lambda _path: ())
    monkeypatch.setattr("remote_agents.composition.backend.discover_projects", lambda _path: ())
    monkeypatch.setattr(
        "remote_agents.composition.backend.build_catalogue", lambda *_args, **_kwargs: ()
    )
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
    monkeypatch.setattr(
        "remote_agents.composition.tui.probe_profiles",
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
    # A green report says the config was compared, rather than leaving the operator to infer
    # it from the absence of a complaint.
    assert report["config"]["readable"] is True
    assert report["config"]["missing"] == [] and report["config"]["unknown"] == []
    assert [profile["status"] for profile in report["profiles"]] == ["AVAILABLE"] * 5


@pytest.mark.asyncio
async def test_private_bot_boundary_renders_and_refreshes_only_issued_owner_callbacks() -> None:
    boundary = build_private_bot(7, 11)
    message = _Message()
    update = _trusted_update(message=message)

    await boundary.start(update, None)

    assert (
        message.replies[0]["text"]
        == "<b>Sessions</b> · 0 total · 0 active · 0 preserved\nNothing is running."
    )
    launch = _button(message.replies[0], "Launch")
    callback = _Callback(launch)
    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None]
    assert callback.edits[0]["text"] == "<b>Projects 1/1</b>\nSelect a project to launch."

    await boundary.callback(_trusted_update(callback=callback), None)

    # The first press replaced this message's keyboard, which pruned the token it came from
    # -- so the second press of the same button is a race, answered and redrawn.
    assert callback.answers == [None, "That screen has moved on."]
    assert callback.edits[1]["text"].startswith("<b>Sessions</b>")


@pytest.mark.asyncio
async def test_private_bot_boundary_launches_on_the_agent_press_and_drops_a_repeat() -> None:
    launcher = _Launcher()
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),), sessions=launcher
        ),
        profiles=(ProfileAvailability("claude", True),),
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = _button(message.replies[0], "Launch")
    projects = _Callback(launch)
    await boundary.callback(_trusted_update(callback=projects), None)
    project = projects.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    profiles = _Callback(project)
    await boundary.callback(_trusted_update(callback=profiles), None)
    profile = profiles.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    launched = _Callback(profile)
    await boundary.callback(_trusted_update(callback=launched), None)

    # Three presses from Home, not five: choosing the agent starts the session, and there is
    # no review screen between the choice and the launch.
    assert len(launcher.commands) == 1
    assert str(launcher.commands[0].project_id) == "a" * 24
    assert str(launcher.commands[0].profile_id) == "claude"
    assert launcher.commands[0].label is None, "a launch is unnamed; naming it comes later"

    # DEC-008: the same button pressed again is dropped, never serviced into a second session.
    await boundary.callback(_trusted_update(callback=_Callback(profile)), None)

    assert len(launcher.commands) == 1, "a second press must not start a second session"


@pytest.mark.asyncio
async def test_failed_launch_explains_that_workspace_trust_is_never_approved_remotely() -> None:
    failed = _record(SessionState.FAILED, "failed", ProjectId("a" * 24))
    launcher = _Launcher()
    launcher.launch_result = failed
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),), sessions=launcher
        ),
        profiles=(ProfileAvailability("cursor-agent", True),),
    )
    token = boundary.callbacks.create(
        "launch.profile", "a" * 24 + "|cursor-agent", 7, 11, 1, mutation=True
    )

    reply = await boundary._launch_reply("a" * 24 + "|cursor-agent", token, 1)

    assert "Session did not become ready" in reply["text"]
    assert "never approved remotely" in reply["text"]
    assert [
        unpadded(button.text) for row in reply["reply_markup"].inline_keyboard for button in row
    ] == [
        "Details",
        "Sessions",
        "Launch",
    ]


@pytest.mark.asyncio
async def test_private_bot_boundary_ignores_a_duplicate_telegram_edit() -> None:
    boundary = build_private_bot(7, 11)
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = _button(message.replies[0], "Launch")
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
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),), sessions=launcher
        ),
    )

    reply = await boundary._sessions_reply()

    labels = tuple(unpadded(button.text) for row in reply.keyboard for button in row)
    assert labels[0].startswith("Demo · codex · regular · #1 · active · running · ")
    assert labels[1:] == ("Sessions", "Launch")
    assert "ended" not in labels


@pytest.mark.asyncio
async def test_private_bot_boundary_searches_projects_and_launches_from_a_result() -> None:
    launcher = _Launcher()
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(
                CatalogProject("a" * 24, "opaque-editor", "writing", "Registered"),
                CatalogProject("b" * 24, "opaque-verse", "writing", "Registered"),
            ),
            sessions=launcher,
        ),
        profiles=(ProfileAvailability("codex", True),),
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = _button(message.replies[0], "Launch")
    projects = _Callback(launch)
    await boundary.callback(_trusted_update(callback=projects), None)
    search = projects.edits[0]["reply_markup"].inline_keyboard[2][0].callback_data
    awaiting_search = _Callback(search)
    await boundary.callback(_trusted_update(callback=awaiting_search), None)
    assert awaiting_search.edits[0]["text"] == "Reply below with a project name."
    assert awaiting_search.sends[0]["reply_markup"].input_field_placeholder == "Project name"

    result = _Message("verse")
    await boundary.text(_trusted_update(message=result), None)

    project = result.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    profiles = _Callback(project)
    await boundary.callback(_trusted_update(callback=profiles), None)
    profile = profiles.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    launched = _Callback(profile)
    await boundary.callback(_trusted_update(callback=launched), None)

    # The search still reaches a launch; what it no longer reaches is a label step. Naming a
    # session moved to the session's own menu, so a launch arrives unnamed.
    assert str(launcher.commands[0].project_id) == "b" * 24
    assert launcher.commands[0].label is None


@pytest.mark.asyncio
async def test_owner_commands_render_only_the_private_chat_surface() -> None:
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),)),
    )
    launch = _Message()
    sessions = _Message()
    help_message = _Message()

    await boundary.launch_command(_trusted_update(message=launch), None)
    await boundary.sessions_command(_trusted_update(message=sessions), None)
    await boundary.help_command(_trusted_update(message=help_message), None)

    assert launch.replies[0]["text"] == "<b>Projects 1/1</b>\nSelect a project to launch."
    assert sessions.replies[0]["text"] == (
        "<b>Sessions</b> · 0 total · 0 active · 0 preserved\nNothing is running."
    )
    # The empty list no longer carries its own Launch: the bar carries that destination on
    # the row directly beneath, and a button duplicating its neighbour reads as a bug.
    assert [
        unpadded(button.text) for button in sessions.replies[0]["reply_markup"].inline_keyboard[0]
    ] == [
        "• Sessions",
        "Launch",
    ]
    # Help is a screen like any other now: it carries a keyboard and names the real actions.
    assert help_message.replies[0]["text"].startswith("<b>Remote agents</b>")
    assert "Stop and close" in help_message.replies[0]["text"]
    help_rows = help_message.replies[0]["reply_markup"].inline_keyboard
    assert [unpadded(button.text) for button in help_rows[-1]] == ["Sessions", "Launch"]


@pytest.mark.asyncio
async def test_inspection_sends_the_existing_oversized_output_as_a_utf8_attachment() -> None:
    session = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))

    async def capture(_session_id):
        return "x" * 5000

    launcher = _Launcher()
    launcher.records = [session]
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher, capture=capture))
    await boundary.start(_trusted_update(message=_Message()), None)
    detail = await boundary._detail_reply(str(session.session_id), 1)
    boundary.callbacks.bind_pending(11, 1)
    inspect = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if unpadded(button.text) == "Inspect"
    )
    callback = _Callback(inspect)

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.edits[0]["text"] == "<pre>Output is attached as UTF-8 text.</pre>"
    # Marked unforwardable: a pane's transcript is exactly the thing that should not be one
    # tap from leaving this private chat.
    assert callback.documents == [
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
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher, capture=capture))
    await boundary.start(_trusted_update(message=_Message()), None)
    detail = await boundary._detail_reply(str(session.session_id), 1)
    boundary.callbacks.bind_pending(11, 1)
    inspect = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if unpadded(button.text) == "Inspect"
    )
    callback = _Callback(inspect)

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.edits[0]["text"] == "Inspection is unavailable."
    assert callback.documents == []


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
    boundary = build_private_bot(
        7, 11, backend=backend_for(catalogue=catalogue), project_page_size=10
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = _button(message.replies[0], "Launch")
    first = _Callback(launch)
    await boundary.callback(_trusted_update(callback=first), None)

    first_page = first.edits[0]["reply_markup"].inline_keyboard
    assert first.edits[0]["text"] == "<b>Projects 1/3</b>\nSelect a project to launch."
    assert [row[0].text for row in first_page[:10]] == [f"Project {number}" for number in range(10)]
    second = _Callback(
        next(
            button.callback_data
            for row in first_page
            for button in row
            if unpadded(button.text) == "Next"
        )
    )
    await boundary.callback(_trusted_update(callback=second), None)

    second_page = second.edits[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in second_page[:10]] == [
        f"Project {number}" for number in range(10, 20)
    ]
    third = _Callback(
        next(
            button.callback_data
            for row in second_page
            for button in row
            if unpadded(button.text) == "Next"
        )
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
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    received: list[TelegramSecrets] = []

    async def serve(secrets: TelegramSecrets, _boundary: PrivateBotBoundary) -> None:
        received.append(secrets)

    monkeypatch.setattr(
        "remote_agents.bootstrap._resolve_serve_secrets",
        lambda _paths: TelegramSecrets("token", 7, 11),
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home",
        lambda _home: _Paths(tmp_path / "sessions.sqlite3"),
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap._private_boundary",
        lambda _config, _connection, _paths, _secrets: ServiceComposition(
            build_private_bot(7, 11), _SilentTerminal(), _SilentReconciler()
        ),
    )

    assert main(["serve", "--config", str(config)], serve_runner=serve) == 0
    assert received == [TelegramSecrets("token", 7, 11)]


def test_serve_ranks_the_catalogue_before_the_first_screen_can_be_drawn(
    tmp_path, monkeypatch
) -> None:
    """The catalogue is ranked at startup, not on the first Refresh the owner happens to press.

    The composition hands the catalogue over in registry order and the ranking is applied on
    refresh, so this is the difference between "Launch opens with your most-used project first"
    and "…after you press Refresh". It shipped as the latter: every ranking test called
    `refresh_catalogue()` itself, so none of them could see that nothing else did.

    Asserted against the boundary the serve runner is *handed*, which is the last point before
    Telegram gets it.
    """
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    older = CatalogProject("a" * 24, "older", "tests", "Registered")
    newer = CatalogProject("b" * 24, "newer", "tests", "Registered")

    class _UsageLauncher(SessionUseCaseDouble):
        async def project_usage(self):
            return [
                ProjectUsage(
                    ProjectId(older.opaque_id), 40, datetime.now(UTC) - timedelta(days=400)
                ),
                ProjectUsage(ProjectId(newer.opaque_id), 2, datetime.now(UTC) - timedelta(days=1)),
            ]

        async def list_sessions(self):
            return []

        async def refresh_readiness(self) -> None:
            return None

    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(older, newer),
            refresh_catalogue=lambda: (older, newer),
            sessions=_UsageLauncher(),
        ),
    )
    served: list[tuple[str, ...]] = []

    async def serve(_secrets: TelegramSecrets, handed: PrivateBotBoundary) -> None:
        served.append(tuple(project.name for project in handed.catalogue))

    monkeypatch.setattr(
        "remote_agents.bootstrap._resolve_serve_secrets",
        lambda _paths: TelegramSecrets("token", 7, 11),
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home",
        lambda _home: _Paths(tmp_path / "sessions.sqlite3"),
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap._private_boundary",
        lambda _config, _connection, _paths, _secrets: ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler()
        ),
    )
    assert boundary.catalogue == (older, newer), "registry order before serve runs"

    assert main(["serve", "--config", str(config)], serve_runner=serve) == 0

    assert served == [("newer", "older")], "ranked before the runner ever saw it"


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

    def ensure_directories(self, **_kwargs) -> None:
        return None

    def require_private_environment(self):
        return None

    def open_database(self, *_args, **_kwargs):
        return _Connection()


class _DoctorPaths:
    """Stands in for `ProductionPaths` in every `doctor` test.

    It carries a real 0600 credential file because `doctor` parses one. Task 2.0 retired
    `EnvironmentFile=` so that exactly one parser reads that file, and added
    `_credential_file_state` to report whether the in-process parser still resolves it -- the
    check that made the retirement safe to do at all. That check calls
    `require_private_environment`, which this stub did not have, so `doctor` raised
    `AttributeError` for every test routed through here between 71b52f8 and this commit.

    A stub that answered `None` would have been worse than the crash: `doctor` would report a
    resolving credential file on a host where nothing had been parsed, which is precisely the
    false green the new check exists to prevent. So the file is real, and the parser really
    reads it.
    """

    def __init__(self, config_path) -> None:
        self.config_path = config_path
        self.home = config_path.parent
        self.environment_path = config_path.parent / "telegram.env"
        self.environment_path.write_text(
            "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=test-token\n"
            "REMOTE_AGENTS_OWNER_USER_ID=7\n"
            "REMOTE_AGENTS_OWNER_CHAT_ID=11\n",
            encoding="utf-8",
        )
        self.environment_path.chmod(0o600)

    def require_private_environment(self):
        return self.environment_path


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


class _Launcher(SessionUseCaseDouble):
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
        return a_verified_force_stop(command.session_id)


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
        self.deletions: list[int] = []
        self.text = text
        self.message_id = message_id
        self.bot = _MessageBot(self)

    def get_bot(self) -> _MessageBot:
        return self.bot


class _MessageBot:
    """The same surface, for an update that arrived as a message rather than a press.

    Both a send and an edit land in the owning double's `replies`, because from a test's
    point of view they are the same event — this screen was drawn in answer to this update.
    Which of the two Telegram performed depends only on whether an anchor already existed.
    """

    def __init__(self, owner: _Message) -> None:
        self._owner = owner

    async def send_message(self, **kwargs: object) -> _Message:
        kwargs.pop("chat_id", None)
        self._owner.replies.append(kwargs)
        # The doubles share one message id: this chat has one live view, so a send answers
        # with the same id every later edit will address.
        return _Message(message_id=self._owner.message_id)

    async def edit_message_text(self, **kwargs: object) -> None:
        kwargs.pop("chat_id", None)
        kwargs.pop("message_id", None)
        self._owner.replies.append(kwargs)

    async def delete_message(self, **kwargs: object) -> None:
        self._owner.deletions.append(int(kwargs["message_id"]))


class _Bot:
    """The chat-addressed surface the live view speaks through.

    Records into the lists the press it belongs to already exposes, so a test still reads
    `callback.edits` however the render reached Telegram. What changed underneath is the
    address: a screen is drawn into a message id in a chat rather than into whatever
    message the update arrived on.
    """

    def __init__(self, owner: _Callback, *, first_id: int = 500) -> None:
        self._owner = owner
        self._next_id = first_id

    async def edit_message_text(self, **kwargs: object) -> None:
        if self._owner.edit_error is not None:
            raise self._owner.edit_error
        self._owner.edits.append(kwargs)

    async def send_message(self, **kwargs: object) -> _Message:
        message = _Message(message_id=self._next_id)
        self._next_id += 1
        self._owner.sends.append(kwargs)
        return message

    async def delete_message(self, **kwargs: object) -> None:
        self._owner.deletions.append(kwargs)

    async def send_document(self, **kwargs: object) -> _Message:
        document = kwargs["document"]
        self._owner.documents.append(
            {
                "document": document.read(),
                "filename": kwargs["filename"],
                "protect_content": kwargs.get("protect_content", False),
            }
        )
        return _Message(message_id=self._owner.message.message_id + 1)


class _Callback:
    def __init__(self, data: str, *, edit_error: Exception | None = None) -> None:
        self.data = data
        self.edit_error = edit_error
        self.answers: list[str | None] = []
        self.alerts: list[bool] = []
        self.edits: list[dict[str, object]] = []
        self.sends: list[dict[str, object]] = []
        self.deletions: list[dict[str, object]] = []
        self.documents: list[dict[str, object]] = []
        self.message = _Message()
        self.bot = _Bot(self)

    def get_bot(self) -> _Bot:
        return self.bot

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append(text)
        self.alerts.append(show_alert)


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
    boundary = build_private_bot(7, 11, backend=backend_for(catalogue=_catalogue(94)))

    resume = boundary._resume_projects_reply()
    launch = boundary._projects_reply(boundary.catalogue, view_id="all")

    assert len(resume.keyboard) == len(launch.keyboard)
    assert resume.text.startswith("<b>Resume 1/10</b>")
    # No Back: the picker is reachable in one press from every screen, so it has no single
    # parent for Back to name, and the bar is the way out.
    assert [[unpadded(button.text) for button in row] for row in resume.keyboard[-3:]] == [
        ["Next"],
        ["Search"],
        ["Sessions", "Launch"],
    ]


def test_a_resume_project_page_stays_inside_the_resume_flow() -> None:
    """A project chosen after paging or searching must still resume, never launch."""
    boundary = build_private_bot(7, 11, backend=backend_for(catalogue=_catalogue(30)))
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
    boundary = build_private_bot(7, 11, backend=backend_for(catalogue=_catalogue(30)))
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
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),), sessions=launcher
        ),
    )
    return boundary, launcher


@pytest.mark.asyncio
async def test_session_detail_offers_a_way_back_and_keeps_the_stops_on_their_own_row() -> None:
    """Shape is the only separator Telegram gives, so the stops must not look like reads."""
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, _ = _stop_boundary(running)

    detail = await boundary._detail_reply(str(running.session_id))

    rows = [[unpadded(button.text) for button in row] for row in detail.keyboard]
    assert rows[0] == ["Inspect"]
    assert rows[-3] == ["Stop and close", "Force stop"]
    assert rows[-2] == ["Back"]
    assert rows[-1] == ["Sessions", "Launch"]


@pytest.mark.asyncio
async def test_no_screen_offers_refresh_now_that_every_route_re_reads() -> None:
    """The button is gone from both screens that carried it, and from nowhere else.

    It was offered on exactly two — Home and the sessions list — because those are the two
    whose answer moves without the owner touching anything. Both re-derive that answer on
    every entry, so what it bought was a tap.
    """
    boundary, _ = _stop_boundary(_record(SessionState.RUNNING, "active", ProjectId("a" * 24)))

    sessions = await boundary._sessions_reply()
    projects = boundary._projects_reply(boundary.catalogue, view_id="all")

    sessions_labels = {unpadded(button.text) for row in sessions.keyboard for button in row}
    project_labels = {unpadded(button.text) for row in projects.keyboard for button in row}
    assert "Refresh" not in sessions_labels
    assert "Refresh" not in project_labels


@pytest.mark.asyncio
async def test_a_refresh_token_drawn_before_the_button_was_removed_still_answers() -> None:
    """Tokens outlive the deploy that stopped drawing them, so `nav.refresh` stays handled.

    A button is valid for the message it was drawn on rather than for a clock, so a Home
    screen rendered before this change still carries a live Refresh. Dropping the case would
    turn it into the dead button the callback store exists to prevent.
    """
    boundary, _ = _stop_boundary(_record(SessionState.RUNNING, "active", ProjectId("a" * 24)))

    reply = await boundary._reply_for("nav.refresh", "home")

    assert "Sessions" in str(reply["text"])
    assert "1 active" in str(reply["text"])


@pytest.mark.asyncio
async def test_back_from_a_session_detail_returns_to_the_page_it_was_opened_from() -> None:
    """The one thing Refresh did that no other route did, now carried by Back.

    `sessions.open` renders the first page by design — it is what Home's Sessions button and
    `/sessions` mean. Back out of a detail is the one case with a known page to return to.
    """
    records = [
        _record(SessionState.RUNNING, f"active-{index}", ProjectId("a" * 24)) for index in range(9)
    ]
    boundary, _ = _stop_boundary(*records)
    boundary.session_page_size = 4

    listing = await boundary._sessions_reply(3)
    assert "Sessions 3/3" in listing.text
    row = next(
        button
        for button_row in listing.keyboard
        for button in button_row
        if unpadded(button.text) not in {"Previous", "Next", "Sessions", "Launch", "Resume"}
    )
    boundary.callbacks.bind_pending(11, 1)
    opened = boundary.callbacks.resolve(row.callback_data, owner_id=7, chat_id=11, message_id=1)
    assert opened is not None
    detail = await boundary._detail_reply(opened.entity_id)

    back = next(
        button for row_ in detail.keyboard for button in row_ if unpadded(button.text) == "Back"
    )
    boundary.callbacks.bind_pending(11, 1)
    state = boundary.callbacks.resolve(back.callback_data, owner_id=7, chat_id=11, message_id=1)

    assert state is not None
    assert state.action == "sessions.page"
    assert state.entity_id == "3"
    returned = await boundary._sessions_reply(int(state.entity_id))
    assert "Sessions 3/3" in returned.text


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

    assert first.text.startswith("<b>Sessions 1/3</b> · ")
    assert [unpadded(button.text) for button in first.keyboard[-2]] == ["Next"]
    assert len(first.keyboard) == 4 + 2
    assert last.text.startswith("<b>Sessions 3/3</b> · ")
    assert [unpadded(button.text) for button in last.keyboard[-2]] == ["Previous"]
    # A page number past the end clamps rather than rendering an empty list.
    assert beyond.text == last.text


@pytest.mark.asyncio
async def test_force_confirmation_names_the_session_and_buffers_the_kill_from_the_bar() -> None:
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, _ = _stop_boundary(running)
    # The name the bot shows carries the catalogue's project name, not the opaque slug.
    subject = (await boundary._record(str(running.session_id))).display.rendered
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, None, "force", 7, 11
    )
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("force", token, 1)

    rows = [
        [unpadded(button.text) for button in row] for row in reply["reply_markup"].inline_keyboard
    ]
    assert subject in reply["text"]
    assert "cannot be undone" in reply["text"]
    # Force stop first, Cancel beneath it: the bar is the bottom row now, so last-but-one is
    # the worst place for an irreversible button rather than a safe one.
    assert rows[0] == ["Force stop"]
    assert rows[1] == ["Cancel"]


@pytest.mark.asyncio
async def test_a_completed_stop_names_the_session_and_what_became_of_its_output() -> None:
    running = _record(SessionState.RUNNING, "active", ProjectId("a" * 24))
    boundary, launcher = _stop_boundary(running)
    subject = (await boundary._record(str(running.session_id))).display.rendered
    token = boundary.stops.offer(
        running.session_id, running.profile_id, running.state, None, "graceful", 7, 11
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
        running.session_id, running.profile_id, running.state, None, "graceful", 7, 11
    )
    boundary.callbacks.bind_pending(11, 1)

    reply = await boundary._stop_reply("graceful", token, 1)

    assert "is still running" in reply["text"]
    assert "did not exit in time" in reply["text"]
    # The outcome now leads the session list rather than a screen of its own. The session is
    # still listed precisely because the stop did not take, so the row the owner needs to act
    # on is already under the notice — which is what the "Open session" button was for, and
    # why this keyboard no longer carries it or the Back that led out of that dead end.
    assert "Sessions 1/1" in reply["text"]
    labels = [
        unpadded(button.text) for row in reply["reply_markup"].inline_keyboard for button in row
    ]
    assert labels[-2:] == ["Sessions", "Launch"]
    assert "Back" not in labels
    assert "Open session" not in labels
    assert labels[0].startswith("Demo"), "the session that would not stop is on the list"


def test_only_the_actions_that_make_the_owner_wait_get_a_pending_notice() -> None:
    boundary = build_private_bot(7, 11)

    assert boundary._pending_notice("graceful") is not None
    # Selecting the agent is the launch now, so the wait it causes is announced there.
    assert boundary._pending_notice("launch.profile") is not None
    assert boundary._pending_notice("launch.confirm") is None, "the review step is gone"
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
        running.session_id, running.profile_id, running.state, None, "graceful", 7, 11
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
    assert "Sessions" in str(callback.edits[0]["text"])


def _trusted_update(*, message: _Message | None = None, callback: _Callback | None = None):
    carrier = callback if callback is not None else message
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=11, type="private"),
        effective_message=message,
        callback_query=callback,
        get_bot=lambda: carrier.get_bot(),
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
        running.session_id, running.profile_id, running.state, None, "graceful", 7, 11
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
    rendered = _reply_arguments(await boundary._sessions_reply())
    sessions = _Callback(_button(rendered, "Sessions"))
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
    rendered = _reply_arguments(await boundary._sessions_reply())
    sessions = _Callback(_button(rendered, "Sessions"))
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
    # Marker-stripped: a bar button carries "• " exactly when the owner is inside that flow,
    # which after /start lands on the sessions list is the common case rather than the rare one.
    return next(
        button.callback_data
        for row in reply["reply_markup"].inline_keyboard
        for button in row
        if unpadded(button.text).removeprefix("• ") == text
    )


def _edited_button(callback: _Callback, index: int, *, text: str | None = None) -> str:
    keyboard = callback.edits[-1]["reply_markup"].inline_keyboard
    if text is not None:
        return next(
            button.callback_data
            for row in keyboard
            for button in row
            if unpadded(button.text) == text
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
    before = build_private_bot(7, 11, callbacks=SQLiteCallbackStateStore(connection))
    message = _Message()
    await before.start(_trusted_update(message=message), None)
    sessions = _button(message.replies[0], "Sessions")
    connection.close()

    after = build_private_bot(7, 11, callbacks=SQLiteCallbackStateStore(open_database(database)))
    callback = _Callback(sessions)
    await after.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None], "the restarted service refused a button it had drawn"
    assert "Sessions" in str(callback.edits[0]["text"])


class _NotifyBot:
    """The chat-addressed surface an *unsolicited* message goes out through.

    Separate from `_Bot` because it answers a different question. Every other double in this
    file records what a press produced; this one records what the service said with nobody
    pressing anything, which is the whole of Stage 3 and the only outbound path in the bot
    that no owner action started.

    `fail_sends` models Telegram being unreachable for the first N sends, because "retried on
    the next pass" is a claim about the failure path and cannot be tested from the happy one.
    """

    def __init__(self, *, first_id: int = 900, fail_sends: int = 0) -> None:
        self.sends: list[dict[str, object]] = []
        self.markups: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self._next_id = first_id
        self._failures = fail_sends

    async def send_message(self, **kwargs: object) -> _Message:
        if self._failures > 0:
            self._failures -= 1
            raise TelegramError("Telegram is unreachable")
        self.sends.append(kwargs)
        message = _Message(message_id=self._next_id)
        self._next_id += 1
        return message

    async def edit_message_reply_markup(self, **kwargs: object) -> None:
        self.markups.append(kwargs)

    async def delete_message(self, **kwargs: object) -> None:
        # A session owns one message, so every report after the first sends a replacement and
        # deletes the message it supersedes. A double without this raised out of the delete,
        # which the notifier now survives -- but it survives it by logging a warning and
        # leaving a message in the chat, so a test that hit it would be measuring the
        # degraded path without saying so.
        self.deletes.append(kwargs)


def _spool(
    directory,
    session_id: str,
    *,
    event: str = "Stop",
    stamp: str = "000001",
    reason: str | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "session_id": session_id,
        "event": event,
        "observed_at": "2026-08-11T14:05:00+00:00",
        "detail": "Ran the suite.",
    }
    # `StopFailure` and `Notification` discriminate on a field of their own, so a test that
    # wants a second *kind* has to be able to set it. Omitted rather than defaulted, because a
    # record carrying a reason the event does not use is not one the hook would ever write.
    if reason is not None:
        record["reason"] = reason
    (directory / f"{session_id}-20260811T140500{stamp}Z.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def _notified(*records: SessionRecord, **bot_arguments: object):
    launcher = _Launcher()
    launcher.records = list(records)
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher))
    bot = _NotifyBot(**bot_arguments)
    boundary.notifier.attach(bot)
    return boundary, bot


def _running(label: str = "one") -> SessionRecord:
    return _record(SessionState.RUNNING, label, ProjectId("p" * 24))


async def test_a_spooled_activity_is_delivered_once_and_leaves_no_file(
    tmp_path,
) -> None:
    """The spool is drained by delivery, not merely read by it.

    Exactly-once here is a property of two things agreeing: the drain deletes what it returns,
    and the notifier sends what the drain returned. A pass that sent without draining would
    repeat the same message every poll for as long as the file sat there.

    Named for the *activity* rather than the message since delivery became grouped: one record
    still reaches the owner once, but a second record from the same session in the same pass
    now rides in the same message rather than a second one, which the old name asserted.
    """
    record = _running()
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))
    composition = ServiceComposition(
        boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
    )

    await _watch_activity_once(composition)

    assert len(bot.sends) == 1
    assert record.display.rendered in str(bot.sends[0]["text"])
    assert list(spool.glob("*.json")) == []

    await _watch_activity_once(composition)
    assert len(bot.sends) == 1, "a second pass re-delivered an activity that was already sent"


async def test_a_restart_over_a_drained_spool_sends_no_notification(tmp_path) -> None:
    """A fresh service is not a fresh spool. Nothing survives a delivery that the next
    process could mistake for undelivered work."""
    record = _running()
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))

    first, first_bot = _notified(record)
    await _watch_activity_once(
        ServiceComposition(first, _SilentTerminal(), _SilentReconciler(), activity_directory=spool)
    )
    assert len(first_bot.sends) == 1

    restarted, restarted_bot = _notified(record)
    await _watch_activity_once(
        ServiceComposition(
            restarted, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert restarted_bot.sends == []


async def test_everything_one_session_says_lands_in_that_session_s_one_message(
    tmp_path,
) -> None:
    """A hook that fires on every turn is a notification storm, not a signal.

    A different kind used to earn a second message, on the reasoning that an agent which
    finishes and then needs an answer has said two different things. It has — and both of them
    belong in the message that session already owns. The owner's report is what settled it:
    what buries the useful signal is not how many *things* a session says, it is how many
    places in the chat it says them.
    """
    record = _running()
    boundary, bot = _notified(record)
    session_id = str(record.session_id)
    spool = tmp_path / "activity"
    for index in range(5):
        _spool(spool, session_id, stamp=f"00000{index}")
    composition = ServiceComposition(
        boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
    )

    await _watch_activity_once(composition)

    assert len(bot.sends) == 1
    _spool(spool, session_id, event="StopFailure", reason="rate_limit", stamp="000009")
    await _watch_activity_once(composition)

    assert len(bot.sends) - len(bot.deletes) == 1, "the second kind opened a second message"
    assert len(bot.deletes) == 1, "the message it replaced was left in the chat"
    replacement = str(bot.sends[-1]["text"])
    assert "finished its work" in replacement
    assert "usage limit" in replacement, "the different kind is in there"


async def test_a_notification_whose_send_fails_is_retried_on_the_next_pass(tmp_path) -> None:
    """The drain has already deleted the file by the time Telegram refuses it, so a dropped
    send is a lost notification — the only copy left is the one held in memory."""
    record = _running()
    boundary, bot = _notified(record, fail_sends=1)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))
    composition = ServiceComposition(
        boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
    )

    await _watch_activity_once(composition)
    assert bot.sends == [], "the double was supposed to refuse the first send"

    await _watch_activity_once(composition)

    assert len(bot.sends) == 1
    assert record.display.rendered in str(bot.sends[0]["text"])


@pytest.mark.parametrize(
    "state",
    [
        SessionState.ENDED,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
    ],
)
async def test_a_session_the_owner_has_already_dealt_with_is_not_notified_about(
    tmp_path, state: SessionState
) -> None:
    """The inverse of what this test asserted before, and the inversion is the point.

    It used to prove that an ENDED record was *still named* by its notification, because
    `SessionEnd` was precisely the kind whose record had ENDED by delivery time and resolving
    the name through the sessions list would have dropped every one of them. That kind is
    retired, and with it the only reason to speak about a session the owner has finished with:
    a `Stop` arriving for a session they already stopped reports their own action back.

    Parametrized over the states rather than pinned to ENDED, because the defect is about the
    whole not-working half of the lifecycle and a check naming one member of it could pass
    while the other three still notified.
    """
    record = _record(state, "finished", ProjectId("q" * 24))
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))

    await _watch_activity_once(
        ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert bot.sends == []


async def test_only_an_actionable_report_about_a_live_session_reaches_the_owner(
    tmp_path,
) -> None:
    """The whole of the value filter, driven through the real drain and the real notifier.

    Each half is already pinned on its own -- the mapping drops `SessionEnd` and the idle
    timer in `tests/unit/application/test_activity.py`, and `_display_for` declines a finished
    session above. Neither of those runs the two together, and the two together are what the
    owner actually experiences: one pass, four spooled records, one message.

    Worth its own test rather than left to the pair because the failure it catches is a seam.
    A mapping that dropped nothing and a liveness check that declined everything would each
    pass their own tests while this one found an empty chat or a full one.
    """
    live = _running()
    finished = _record(SessionState.ENDED, "finished", ProjectId("q" * 24))
    boundary, bot = _notified(live, finished)
    spool = tmp_path / "activity"
    _spool(spool, str(live.session_id), event="SessionEnd", stamp="000001")
    _spool(spool, str(live.session_id), event="Notification", reason="idle_prompt", stamp="000002")
    _spool(spool, str(finished.session_id), stamp="000003")
    _spool(spool, str(live.session_id), stamp="000004")

    await _watch_activity_once(
        ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert len(bot.sends) == 1, "only the Stop from the running session is worth sending"
    assert live.display.rendered in str(bot.sends[0]["text"])
    assert "finished its work" in str(bot.sends[0]["text"])
    assert list(spool.iterdir()) == [], "a dropped record is still drained off disk"


async def test_a_session_that_stops_while_its_notification_waits_is_not_notified(
    tmp_path,
) -> None:
    """Liveness is read when the message is sent, not when the record was drained.

    The gap is real and is exactly where the owner's complaint lives: the drain deletes a
    record before returning it, a refused send leaves that activity in the retry queue, and
    the owner presses Stop while it sits there. Checking at drain time would have found a
    RUNNING session and sent the message a pass later anyway.
    """
    record = _running()
    boundary, bot = _notified(record, fail_sends=1)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))
    composition = ServiceComposition(
        boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
    )

    await _watch_activity_once(composition)
    assert bot.sends == [], "the double was supposed to refuse the first send"

    # The owner presses Stop while the activity is held for retry. Nothing re-drains it; the
    # only copy left is the one in the notifier's queue.
    boundary.backend.sessions.records = [replace(record, state=SessionState.STOP_REQUESTED)]

    await _watch_activity_once(composition)

    assert bot.sends == []


async def test_a_watched_pane_reaches_the_owner_as_a_notification(tmp_path) -> None:
    """The other half of the source. Stage 2 computed this and dropped it on purpose; the
    drop is what this pass exists to end, so it is pinned rather than left to a docstring.

    The observation the watcher makes changed on 2026-08-30 -- a pane digest going quiet became
    a Codex approval marker appearing -- and what this pins did not: an activity produced by the
    watcher rather than by the spool still reaches the notifier and is sent."""
    record = _running()
    boundary, bot = _notified(record)

    class _ApprovalWatcher:
        def __init__(self) -> None:
            self.passes = 0

        def mark_needs_answer_reported(self, session_ids: tuple[str, ...]) -> None:
            assert session_ids == ()

        async def poll(self):
            self.passes += 1
            return (
                AgentActivity(
                    session_id=str(record.session_id),
                    kind=ActivityKind.NEEDS_ANSWER,
                    detail=None,
                    observed_at=datetime(2026, 8, 11, 14, 5, tzinfo=UTC),
                    confidence=ActivityConfidence.INFERRED,
                ),
            )

    watcher = _ApprovalWatcher()
    await _watch_activity_once(
        ServiceComposition(boundary, _SilentTerminal(), _SilentReconciler(), watcher)
    )

    assert watcher.passes == 1
    assert len(bot.sends) == 1
    assert "waiting for an answer" in str(bot.sends[0]["text"]).lower()


async def test_a_notification_is_not_the_live_view_and_keeps_its_own_keyboard(tmp_path) -> None:
    """Its button is bound to the message the send answered with, never to the anchor.

    The token is minted *after* the send for that reason: `bind_pending` adopts every unbound
    token in the chat, so a token minted before an awaited send can be claimed by a render
    that interleaved with it — and then the notification's one button is bound to the live
    view, where nothing draws it.
    """
    record = _running()
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))

    await _watch_activity_once(
        ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert boundary.view.anchor() is None, "a notification took over the chat's live view"
    assert len(bot.markups) == 1
    keyboard = bot.markups[0]["reply_markup"].inline_keyboard
    assert [unpadded(button.text) for row in keyboard for button in row] == ["Open session"]
    token = keyboard[0][0].callback_data
    assert (
        boundary.callbacks.resolve(
            token, owner_id=7, chat_id=11, message_id=int(bot.markups[0]["message_id"])
        )
        is not None
    )


async def test_a_failing_drain_does_not_discard_a_watched_notification_already_computed(
    tmp_path, monkeypatch
) -> None:
    """The two sources are guarded separately because one of them commits before it returns.

    `poll()` records the approval marker as seen as it decides the marker just appeared, and
    re-arms only when the title clears again. Under one shared `try`, a drain that raised after
    a successful poll threw that observation away with its edge state already committed — so
    that approval became unreportable, permanently, and nothing counted anything as lost. The
    owner simply never hears that the agent is waiting on them.
    """
    record = _running()
    boundary, bot = _notified(record)

    class _ApprovalWatcher:
        def mark_needs_answer_reported(self, session_ids: tuple[str, ...]) -> None:
            assert session_ids == ()

        async def poll(self):
            return (
                AgentActivity(
                    session_id=str(record.session_id),
                    kind=ActivityKind.NEEDS_ANSWER,
                    detail=None,
                    observed_at=datetime(2026, 8, 11, 14, 5, tzinfo=UTC),
                    confidence=ActivityConfidence.INFERRED,
                ),
            )

    def _explode(_directory):
        raise RuntimeError("the spool could not be listed")

    monkeypatch.setattr("remote_agents.composition.service.drain_activity", _explode)

    await _watch_activity_once(
        ServiceComposition(
            boundary,
            _SilentTerminal(),
            _SilentReconciler(),
            _ApprovalWatcher(),
            activity_directory=tmp_path / "activity",
        )
    )

    assert len(bot.sends) == 1, "the drain's failure took the watcher's observation with it"
    assert "waiting for an answer" in str(bot.sends[0]["text"]).lower()


async def test_a_pass_that_observes_nothing_still_retries_a_held_notification(tmp_path) -> None:
    """Returning early on an empty pass would strand a backlog until something else happened.

    The retry queue is drained by `deliver`, not by the sources, so a quiet host with a held
    notification must still deliver it — which is exactly the state a Telegram outage leaves.
    """
    record = _running()
    boundary, bot = _notified(record, fail_sends=1)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))
    composition = ServiceComposition(
        boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
    )

    await _watch_activity_once(composition)
    assert bot.sends == []
    assert boundary.notifier.pending_count() == 1

    # Nothing new to observe: the spool is empty and there is no approval watcher.
    await _watch_activity_once(composition)

    assert len(bot.sends) == 1
    assert boundary.notifier.pending_count() == 0


def _doctor_config_text(tmp_path, limits: str) -> str:
    return (
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        f"[limits]\n{limits}"
    )


def _arrange_doctor(tmp_path, monkeypatch, config_text: str) -> None:
    """Wire `doctor` exactly as the healthy-path test does, but over a given config."""
    config = tmp_path / "config.toml"
    config.write_text(config_text, encoding="utf-8")
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
    monkeypatch.setattr(
        "remote_agents.composition.backend.load_registry",
        lambda _path: SimpleNamespace(projects=(), error=None),
    )
    monkeypatch.setattr("remote_agents.bootstrap.discover_projects", lambda _path: ())
    monkeypatch.setattr("remote_agents.composition.backend.discover_projects", lambda _path: ())
    monkeypatch.setattr(
        "remote_agents.composition.backend.build_catalogue", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.probe_profiles",
        lambda *_args, **_kwargs: (_compatibility("claude"),),
    )
    monkeypatch.setattr(
        "remote_agents.composition.tui.probe_profiles",
        lambda *_args, **_kwargs: (_compatibility("claude"),),
    )


def test_doctor_stale_config_missing_key_reports_the_drift_it_was_built_to_diagnose(
    tmp_path, monkeypatch, capsys
) -> None:
    """The real incident: a deployed config that predates two keys the code now requires.

    `docs/acceptance-2026-08-11-agent-activity.md:268-281` records the service crash-looping
    through three restarts on exactly this, and `doctor` -- the command an operator runs
    *before* trusting a deploy -- died the same way, with a traceback instead of a diagnosis.
    """
    _arrange_doctor(
        tmp_path,
        monkeypatch,
        _doctor_config_text(tmp_path, "max_label_length = 40\nproject_page_size = 10\n"),
    )

    assert main(["doctor", "--json"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is False
    assert report["config"]["readable"] is False
    # Nothing else was probed -- the registry and database paths are read *out of* the config
    # that would not load -- so nothing else is claimed. A report asserting six component
    # failures nobody looked for would send an operator chasing phantoms behind one fault.
    assert report["checked"] is False
    assert report["components"] == {}
    # Naming the keys is the whole point: the runbook fix is a line of TOML, and a report that
    # says only "config_schema_drift" sends the operator back to the runbook to find out which.
    # `activity_quiet_polls` was in this set until it was retired on 2026-08-30; a retired key
    # is never missing, which is the whole of what retiring one buys.
    assert set(report["config"]["missing"]) == {"activity_poll_seconds"}


def test_doctor_stale_config_unknown_key_reports_the_drift_rather_than_raising(
    tmp_path, monkeypatch, capsys
) -> None:
    """The other direction of the same drift: a key the code used to have and dropped.

    A rollback produces this as readily as an upgrade produces the missing-key case --
    `_require_exact_keys` refuses both -- so a check proven only on the incident that
    happened to occur would leave `doctor` crashing on the one that happens next.
    """
    _arrange_doctor(
        tmp_path,
        monkeypatch,
        _doctor_config_text(
            tmp_path,
            "max_label_length = 40\nproject_page_size = 10\n"
            "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n"
            "retired_knob = 7\n",
        ),
    )

    assert main(["doctor", "--json"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is False
    assert report["config"]["unknown"] == ["retired_knob"]
    assert report["config"]["missing"] == []


def test_doctor_stale_config_out_of_bounds_value_reports_the_drift_rather_than_raising(
    tmp_path, monkeypatch, capsys
) -> None:
    """The third way `load_config` refuses: every key present, one value out of range.

    Structurally complete and still unloadable. This is the case a key-set comparison alone
    cannot see, which is why the drift check asks `load_config` itself rather than reasoning
    about keys and stopping there.
    """
    _arrange_doctor(
        tmp_path,
        monkeypatch,
        _doctor_config_text(
            tmp_path,
            "max_label_length = 4000\nproject_page_size = 10\n"
            "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        ),
    )

    assert main(["doctor", "--json"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is False
    assert report["config"]["unknown"] == []
    assert report["config"]["missing"] == []
    # No key is wrong, so the key lists are empty and `invalid` is the only thing carrying the
    # diagnosis. A report that said nothing here would be a report that called an unloadable
    # config fine.
    assert report["config"]["invalid"]
    assert "max_label_length" in report["config"]["invalid"][0]


async def test_one_session_saying_several_things_in_a_pass_gets_one_message(tmp_path) -> None:
    """The owner's complaint, end to end: an agent that says three things is not three alerts.

    Driven through the real drain and the real notifier rather than the grouping function,
    because the two halves have to agree about what a pass *is* -- the drain returns a batch
    ordered by observation time and the notifier groups whatever is in its queue, and a version
    that grouped only within one drain's batch would pass a unit test and still send a second
    message for anything held from an earlier pass.
    """
    record = _running()
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    session_id = str(record.session_id)
    _spool(spool, session_id, stamp="000001")
    _spool(spool, session_id, event="StopFailure", reason="rate_limit", stamp="000002")
    _spool(spool, session_id, event="Notification", reason="permission_prompt", stamp="000003")

    await _watch_activity_once(
        ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert len(bot.sends) == 1, "three observations, one session, one message"
    text = str(bot.sends[0]["text"])
    assert text.count("•") == 3
    assert record.display.rendered in text
    assert "finished its work" in text
    assert "usage limit" in text
    assert "waiting for an answer" in text


async def test_two_sessions_in_one_pass_get_one_message_each(tmp_path) -> None:
    """Grouping is per session, not per pass. Collapsing across sessions would put two agents'
    news under one name, which is worse than the flood it would be fixing."""
    first = _running("one")
    second = _record(SessionState.RUNNING, "two", ProjectId("r" * 24))
    boundary, bot = _notified(first, second)
    spool = tmp_path / "activity"
    # Two observations each, deliberately: with one apiece this test would pass unchanged under
    # the retired one-message-per-observation delivery, and so would prove nothing about
    # grouping at all.
    _spool(spool, str(first.session_id), stamp="000001")
    _spool(spool, str(second.session_id), stamp="000002")
    _spool(spool, str(first.session_id), event="StopFailure", reason="rate_limit", stamp="000003")
    _spool(
        spool,
        str(second.session_id),
        event="Notification",
        reason="permission_prompt",
        stamp="000004",
    )

    await _watch_activity_once(
        ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert len(bot.sends) == 2, "four observations, two sessions, two messages"
    named = {str(send["text"]).split("\n")[0] for send in bot.sends}
    assert len(named) == 2, "each message is headed by its own session"
    for send in bot.sends:
        assert str(send["text"]).count("•") == 2, "each session's own two, and no one else's"


async def test_the_same_thing_said_twice_in_one_pass_is_shown_once(tmp_path) -> None:
    """A `Stop` hook fires per turn, so a long instruction spools the same sentence repeatedly.

    The rate limit already collapses a burst *across* passes; this is the within-pass half,
    where every copy arrives in one drain and no window has elapsed between them.
    """
    record = _running()
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    for index in range(4):
        _spool(spool, str(record.session_id), stamp=f"00000{index}")

    await _watch_activity_once(
        ServiceComposition(
            boundary, _SilentTerminal(), _SilentReconciler(), activity_directory=spool
        )
    )

    assert len(bot.sends) == 1
    text = str(bot.sends[0]["text"])
    assert text.count("finished its work") == 1, "four identical reports are one line"
    assert "•" not in text, "one surviving observation renders in the ungrouped shape"


async def test_a_drained_observation_is_durable_before_it_is_delivered(tmp_path) -> None:
    """The feed's table records what was observed even when Telegram refuses the send.

    Recorded before deliver, deliberately: a failed append costs the feed one row and never
    costs the phone its notification, and a refused send never un-records the observation —
    the two consumers of one observation are independent (DEC-026 stays a statement about
    the notifier's own in-memory state, not about this table).
    """
    from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
    from remote_agents.adapters.sqlite.database import open_database

    record = _running()
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id))
    connection = open_database(tmp_path / "state.sqlite3")
    store = SQLiteActivityStore(connection)
    try:
        composition = ServiceComposition(
            boundary,
            _SilentTerminal(),
            _SilentReconciler(),
            activity_directory=spool,
            activity_store=store,
        )
        await _watch_activity_once(composition)

        recent = await store.recent(limit=10)
        assert len(recent) == 1
        assert recent[0].session_id == str(record.session_id)
        assert len(bot.sends) == 1
    finally:
        connection.close()


async def test_codex_permission_request_reaches_both_feed_and_notification(tmp_path) -> None:
    from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
    from remote_agents.adapters.sqlite.database import open_database

    record = _running()
    boundary, bot = _notified(record)
    spool = tmp_path / "activity"
    _spool(spool, str(record.session_id), event="PermissionRequest")
    connection = open_database(tmp_path / "codex.sqlite3")
    store = SQLiteActivityStore(connection)
    try:
        await _watch_activity_once(
            ServiceComposition(
                boundary,
                _SilentTerminal(),
                _SilentReconciler(),
                activity_directory=spool,
                activity_store=store,
            )
        )
        (activity,) = await store.recent(limit=10)
        assert activity.kind is ActivityKind.NEEDS_ANSWER
        assert len(bot.sends) == 1
    finally:
        connection.close()


def test_serve_closes_the_database_when_secret_resolution_fails(tmp_path, monkeypatch) -> None:
    """The database is opened before the credential is resolved, so the close must be guarded.

    Resolution raises on a partial environment or a credential file that fails its 0600/owner
    guard, and by then the connection is already open. When the resolving call sat above the
    `try`, that exception skipped `finally` and left it unclosed -- a regression introduced by
    threading the credential through as a parameter, and invisible to every other test here
    because they all resolve successfully.
    """
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    closed: list[bool] = []

    class _SpyConnection(_Connection):
        def close(self) -> None:
            closed.append(True)

    class _SpyPaths(_Paths):
        def open_database(self, *_args, **_kwargs):
            return _SpyConnection()

    def _refuse(_paths):
        raise ConfigError("Telegram environment file is missing")

    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home",
        lambda _home: _SpyPaths(tmp_path / "sessions.sqlite3"),
    )
    monkeypatch.setattr("remote_agents.bootstrap._resolve_serve_secrets", _refuse)

    with pytest.raises(ConfigError):
        main(["serve", "--config", str(config)])

    assert closed == [True], "the open connection must be closed when resolution refuses"
