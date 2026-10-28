"""Durable folder ownership through catalogue, manual selection and restart recovery."""

import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_permission_presets import CODEX, PROJECT, Rig, conversation

from remote_agents.adapters.agents.codex.sessions import CodexSessionCatalogue
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.folder_bindings import SQLiteFolderBindings
from remote_agents.adapters.sqlite.interrupted_resumes import SQLiteInterruptedResumes
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.application.commands import ResumeCommand
from remote_agents.application.interrupted_resume import (
    InterruptedResumePass,
    RestartSnapshot,
    SessionAtRestart,
)
from remote_agents.application.session_folders import SessionFolders
from remote_agents.domain.models import ProjectId, SessionId, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.state_machine import LifecycleEvent

OTHER = ProjectId("other-project")
NOW = datetime(2026, 10, 28, tzinfo=UTC)


def checkout(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", "--template=", str(path)], check=True)
    return path.resolve()


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    value.project = checkout(tmp_path / "project")
    value.outside = checkout(tmp_path / "outside")
    value.worktrees = SQLiteWorktreeStore(value.connection)
    value.folders = SQLiteFolderBindings(value.connection, accept=acceptable_root)
    value.authority = SessionFolders({PROJECT: value.project}, value.worktrees, value.folders)
    value.service._project_paths = value.authority._project_paths
    value.service._worktrees = value.worktrees
    value.service._folder_bindings = value.folders
    value.launched_folders = []
    original = value.terminal.resume

    async def resume(session_id, project_id, profile_id, source_id, **kwargs):
        value.launched_folders.append(await value.authority.folder_for(session_id, project_id))
        return await original(session_id, project_id, profile_id, source_id, **kwargs)

    value.terminal.resume = resume
    yield value
    value.connection.close()


async def ended_binding(rig, *, project=PROJECT, preset=PermissionPreset.ASK):
    original = await rig.service.resume(
        ResumeCommand(project, CODEX, conversation(), "original", permissions=preset)
    )
    await rig.folders.bind(original.record.session_id, rig.outside)
    await rig.store.record_event(
        original.record.session_id, LifecycleEvent.RECONCILED_TERMINAL_MISSING
    )
    rig.launched_folders.clear()
    return original.record


class Threads:
    def __init__(self, folder):
        self.folder = folder

    async def list_threads(self, *, cwd, cursor, limit):
        rows = [{"id": "source-123", "updatedAt": 1793145600}] if cwd == self.folder else []
        return {"data": rows, "nextCursor": None}


async def catalogue(rig, client=None):
    return CodexSessionCatalogue(
        {PROJECT: rig.project},
        client or Threads(rig.outside),
        worktree_folders=rig.authority.worktree_folders,
    )


async def test_ended_outside_binding_is_discovered_and_resumed_with_its_saved_preset(rig):
    await ended_binding(rig)
    await rig.settings.set(PermissionPreset.FULL_ACCESS)
    catalog = await catalogue(rig)
    page = await catalog.list_conversations(
        profile_id=CODEX, project_id=PROJECT, page=1, page_size=10
    )
    assert len(page.conversations) == 1
    resolved = await catalog.resolve_conversation(page.conversations[0].reference)
    assert resolved.folder == rig.outside
    resumed = await rig.service.resume(ResumeCommand(PROJECT, CODEX, resolved, "manual"))
    assert resumed.record.project_id == PROJECT
    assert rig.launched_folders == [rig.outside]
    assert await rig.bindings.for_session(resumed.record.session_id) == PermissionPreset.ASK
    replay = await rig.service.resume(ResumeCommand(PROJECT, CODEX, resolved, "replay"))
    assert replay.record.session_id == resumed.record.session_id
    assert rig.launched_folders == [rig.outside]


@pytest.mark.parametrize("change", ["deleted", "moved", "symlink", "not-git"])
async def test_stale_selection_refuses_changed_folder_without_launching_in_project(rig, change):
    await ended_binding(rig)
    selected = replace(conversation(), folder=rig.outside)
    if change == "not-git":
        (rig.outside / ".git").rename(rig.outside / "old-git")
    else:
        moved = rig.outside.with_name("moved")
        rig.outside.rename(moved)
        if change == "symlink":
            rig.outside.symlink_to(moved, target_is_directory=True)
    outcome = await rig.service.resume(ResumeCommand(PROJECT, CODEX, selected, "stale"))
    assert outcome.record.state == SessionState.FAILED
    assert not rig.launched_folders
    assert await rig.authority.worktree_folders(PROJECT) == ()


async def test_persisted_folder_cannot_be_resumed_as_another_project(rig):
    await ended_binding(rig)
    selected = replace(
        conversation(),
        summary=replace(conversation().summary, project_id=OTHER),
        folder=rig.outside,
    )
    outcome = await rig.service.resume(ResumeCommand(OTHER, CODEX, selected, "wrong-project"))
    assert outcome.record.state == SessionState.FAILED
    assert not rig.launched_folders


async def test_duplicate_bindings_and_provider_rows_offer_one_conversation(rig):
    record = await ended_binding(rig)
    duplicate = replace(record, session_id=SessionId.new())
    await rig.store.save(duplicate)
    await rig.folders.bind(duplicate.session_id, rig.outside)
    assert await rig.authority.worktree_folders(PROJECT) == (rig.outside,)

    class DuplicateThreads(Threads):
        async def list_threads(self, **kwargs):
            data = await super().list_threads(**kwargs)
            data["data"] *= 2
            return data

    catalog = await catalogue(rig, DuplicateThreads(rig.outside))
    page = await catalog.list_conversations(
        profile_id=CODEX, project_id=PROJECT, page=1, page_size=10
    )
    assert len(page.conversations) == 1


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_restart_reopens_exact_bound_folder_once_with_recorded_preset(rig, preset):
    record = await ended_binding(rig, preset=preset)
    snapshot = RestartSnapshot(
        (SessionAtRestart(replace(record, state=SessionState.RUNNING), False),),
        None,
        True,
        frozenset(),
        {str(record.session_id): "source-123"},
        (),
        frozenset({CODEX}),
    )
    await rig.settings.set(
        PermissionPreset.FULL_ACCESS
        if preset != PermissionPreset.FULL_ACCESS
        else PermissionPreset.ASK
    )
    outcomes = SQLiteInterruptedResumes(rig.connection)
    runner = InterruptedResumePass(
        outcomes,
        resume=rig.service.resume,
        folder_of=rig.worktrees.folder_for,
        bound_folder_of=rig.folders.folder_for,
        key_claimed=rig.store.idempotency_key_claimed,
        send=rig.terminal.send_prompt,
        now=lambda: NOW,
    )
    (report,) = await runner.run(snapshot)
    assert report.outcome.startswith("resumed:")
    assert report.new_session_id is not None
    assert rig.launched_folders == [rig.outside]
    assert await rig.bindings.for_session(report.new_session_id) == preset
    assert await runner.run(snapshot) == ()
    assert rig.launched_folders == [rig.outside]


async def test_reopened_database_retains_legacy_session_project_association(rig, tmp_path):
    record = await ended_binding(rig)
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        bindings = SQLiteFolderBindings(connection, accept=acceptable_root)
        authority = SessionFolders(
            {PROJECT: rig.project, OTHER: rig.outside}, SQLiteWorktreeStore(connection), bindings
        )
        assert await authority.worktree_folders(PROJECT) == (rig.outside,)
        assert await authority.worktree_folders(OTHER) == ()
        assert await bindings.folder_for(record.session_id) == rig.outside
    finally:
        connection.close()


@pytest.mark.parametrize("surface", ["telegram", "tui"])
async def test_both_resume_surfaces_offer_the_real_folder_catalogue(rig, surface):
    from backends import backend_for

    from remote_agents.application.conversations import ConversationService
    from remote_agents.application.project_catalog import CatalogProject

    await ended_binding(rig)
    catalog = await catalogue(rig)
    conversations = ConversationService(catalog)
    project = CatalogProject(str(PROJECT), "fixture", "infra", "Registered")
    backend = backend_for(
        sessions=rig.service,
        projects=object(),
        refresh_catalogue=lambda: (project,),
        conversations=conversations,
        catalogue=(project,),
    )
    if surface == "telegram":
        from remote_agents.adapters.telegram.service import build_private_bot

        boundary = build_private_bot(7, 11, backend=backend)
        reply = await boundary._resume_catalogue_reply(f"{PROJECT}|codex|1")
        boundary.callbacks.bind_pending(11, 100)
        offered = set()
        for row in reply.keyboard:
            for button in row:
                state = boundary.callbacks.resolve(
                    button.callback_data, owner_id=7, chat_id=11, message_id=100
                )
                if state is not None and state.action == "resume.confirm":
                    offered.add(state.entity_id)
    else:
        from textual.widgets import OptionList

        from remote_agents.adapters.tui.app import RemoteAgentsTui
        from remote_agents.adapters.tui.context import TuiContext
        from remote_agents.application.profiles import ProfileAvailability

        app = RemoteAgentsTui(
            TuiContext(
                backend=backend,
                profiles=(ProfileAvailability("codex", True),),
                attach_argv=lambda session: ("tmux", "attach-session", "-t", f"={session}"),
            )
        )
        async with app.run_test() as pilot:
            await app.action_resume()
            await pilot.pause()
            await app.screen.choose(str(PROJECT))
            await pilot.pause()
            await app.screen.choose("codex")
            await pilot.pause()
            offered = {
                option.id
                for option in app.screen.query_one("#choices", OptionList).options
                if option.id and option.id.startswith("c-")
            }
    assert len(offered) == 1
    from remote_agents.domain.conversations import ConversationReference

    resolved = await conversations.resolve_for_resume(ConversationReference(next(iter(offered))))
    assert resolved.folder == rig.outside


async def test_restart_with_a_deleted_folder_records_named_refusal_and_no_pane(rig):
    record = await ended_binding(rig)
    rig.outside.rename(rig.outside.with_name("moved"))
    runner = InterruptedResumePass(
        SQLiteInterruptedResumes(rig.connection),
        resume=rig.service.resume,
        folder_of=rig.worktrees.folder_for,
        bound_folder_of=rig.folders.folder_for,
        key_claimed=rig.store.idempotency_key_claimed,
        send=rig.terminal.send_prompt,
        now=lambda: NOW,
    )
    snapshot = RestartSnapshot(
        (SessionAtRestart(replace(record, state=SessionState.RUNNING), False),),
        None,
        True,
        frozenset(),
        {str(record.session_id): "source-123"},
        (),
        frozenset({CODEX}),
    )
    (report,) = await runner.run(snapshot)
    assert report.outcome == "skipped:folder_unavailable"
    assert not rig.launched_folders
    assert await runner.run(snapshot) == ()


async def test_regular_catalogue_selection_refuses_a_moved_project_root(rig):
    catalog = await catalogue(rig, Threads(rig.project))
    page = await catalog.list_conversations(
        profile_id=CODEX, project_id=PROJECT, page=1, page_size=10
    )
    selected = await catalog.resolve_conversation(page.conversations[0].reference)
    assert selected.project_root == rig.project
    rig.authority._project_paths[PROJECT] = checkout(rig.project.with_name("new-project"))
    outcome = await rig.service.resume(ResumeCommand(PROJECT, CODEX, selected, "moved-project"))
    assert outcome.record.state == SessionState.FAILED
    assert outcome.folder_unavailable
    assert not rig.launched_folders


@pytest.mark.parametrize("mode", ["manual", "restart"])
@pytest.mark.parametrize("change", ["deleted", "moved", "symlink", "not-git"])
async def test_worktree_replacement_is_refused_before_manual_or_restart_launch(rig, mode, change):
    from remote_agents.ports.worktree_store import Worktree

    record = await ended_binding(rig)
    await rig.worktrees.record(
        Worktree(rig.outside, PROJECT, "feature", "main", "a" * 40, {}, NOW, None),
        bound_to=record.session_id,
    )
    if change == "not-git":
        (rig.outside / ".git").rename(rig.outside / "old-git")
    else:
        moved = rig.outside.with_name("moved")
        rig.outside.rename(moved)
        if change == "symlink":
            rig.outside.symlink_to(moved, target_is_directory=True)
    if mode == "manual":
        result = await rig.service.resume(
            ResumeCommand(PROJECT, CODEX, replace(conversation(), folder=rig.outside), "worktree")
        )
        assert result.record.state == SessionState.FAILED
        assert result.worktree_removed
    else:
        runner = InterruptedResumePass(
            SQLiteInterruptedResumes(rig.connection),
            resume=rig.service.resume,
            folder_of=rig.worktrees.folder_for,
            bound_folder_of=rig.folders.folder_for,
            key_claimed=rig.store.idempotency_key_claimed,
            send=rig.terminal.send_prompt,
            now=lambda: NOW,
        )
        snapshot = RestartSnapshot(
            (SessionAtRestart(replace(record, state=SessionState.RUNNING), False),),
            None,
            True,
            frozenset(),
            {str(record.session_id): "source-123"},
            (),
            frozenset({CODEX}),
        )
        (report,) = await runner.run(snapshot)
        assert report.outcome == "skipped:worktree_removed"
    assert not rig.launched_folders
    assert await rig.authority.folder_for(record.session_id, PROJECT) is None
    assert await rig.authority.worktree_folders(PROJECT) == ()
