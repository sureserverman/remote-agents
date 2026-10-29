"""A real built writer's frozen destination reaches durable project routing."""

import os
from pathlib import Path

import pytest
from cross_project_writer import WriterJourney

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.agents.reported_roots import FileReportedRoots
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.folder_bindings import SQLiteFolderBindings
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.workflow.handoff_envelopes import FileHandoffEnvelopes
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.permission_presets import PermissionSettings, SessionPermissions
from remote_agents.application.services import SessionService
from remote_agents.application.session_folders import SessionFolders
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.rollover import RolloverState


@pytest.fixture(params=("claude", "codex"))
def journey(tmp_path, request):
    checkout = os.environ.get("RA_STAGED_PLANNING")
    if not checkout:
        pytest.skip("RA_STAGED_PLANNING unset: no built planning writer checkout")
    return WriterJourney(tmp_path, Path(checkout), request.param)


def test_actual_writer_v2_destination_is_read_without_reinterpreting_plan_prose(journey):
    journey.run()
    events = FileHandoffEnvelopes().events(journey.source)
    assert len(events) == 1
    ready = events[0]
    assert ready.version == 2
    assert ready.destination.root == journey.destination
    assert ready.destination.plan == str(journey.target_plan)
    assert ready.destination.branch == "feature"
    assert ready.destination.locator == "Task 1.1"
    assert ready.source_root == journey.source
    assert ready.predecessor_session_id == journey.predecessor


class RoutedTerminal(FakeTerminal):
    def __init__(self):
        super().__init__()
        self.idle = True
        self.started = []

    async def pane_idle(self, session):
        return self.idle

    async def launch(self, session_id, project_id, profile_id, **kw):
        folder = await self.folders.folder_for(session_id, project_id)
        preset = await self.bindings.for_session(session_id)
        assert preset == kw.get("permissions")
        self.started.append((session_id, project_id, folder, preset))
        return await super().launch(session_id, project_id, profile_id, **kw)


class RoutedRig:
    def __init__(self, journey):
        self.journey = journey
        self.connection = open_database(journey.base / "domain.sqlite3")
        self.sessions = SQLiteSessionStore(self.connection)
        self.rows = SQLiteRolloverStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        self.folders = SQLiteFolderBindings(self.connection, accept=acceptable_root)
        self.bindings = SQLitePermissionBindings(self.connection)
        self.projects = {
            ProjectId("orchard"): journey.source,
            ProjectId("river"): journey.destination,
        }
        self.config = journey.base / "config.toml"
        self.config.write_text('[codex]\ndefault_permissions = "ask"\n')
        self.settings = PermissionSettings(
            ConfigPermissionSetting(self.config),
            profiles={ProfileId("codex"): tuple(PermissionPreset)},
        )
        self.terminal = RoutedTerminal()
        self.terminal.folders = SessionFolders(self.projects, self.worktrees, self.folders)
        self.terminal.bindings = self.bindings
        self.service = SessionService(
            self.sessions,
            self.terminal,
            worktrees=self.worktrees,
            folder_bindings=self.folders,
            permissions=SessionPermissions(self.settings, self.bindings, self.sessions),
        )
        self.reported = FileReportedRoots(journey.base / "activity", accept=acceptable_root)

    def build(self):
        async def enabled():
            return True

        return build_rollover_pass(
            connection=self.connection,
            sessions=self.service,
            terminal=self.terminal,
            enabled=enabled,
            project_paths=self.projects,
            adopt_templates=adopt_templates(),
            worktrees=self.worktrees,
            folders=self.folders,
            reported=self.reported,
        )

    async def start(self):
        outcome = await self.service.launch(
            LaunchCommand(ProjectId("orchard"), ProfileId("codex"), "source")
        )
        self.predecessor = outcome.record.session_id
        self.journey.predecessor = str(self.predecessor)
        self.journey.run()
        self.pass_ = self.build()
        return self

    async def row(self):
        return await self.rows.latest_for(self.predecessor)


@pytest.fixture
async def routed(journey):
    rig = await RoutedRig(journey).start()
    try:
        yield rig
    finally:
        rig.connection.close()


async def test_real_composition_persists_destination_and_preset_before_one_launch(routed):
    rig = routed
    rig.terminal.idle = False
    await rig.pass_.run_once()
    frozen = await rig.row()
    assert frozen.source_root == rig.journey.source
    assert frozen.target.project_id == ProjectId("river")
    assert frozen.target.destination.root == rig.journey.destination
    assert frozen.target.permissions is PermissionPreset.ASK
    # Defaults and root reports after the transaction cannot retarget its destination or preset.
    await rig.settings.set(PermissionPreset.FULL_ACCESS)
    third = rig.journey.repo("summit")
    assert rig.reported.record(str(rig.predecessor), third)
    rig.terminal.idle = True
    restarted = rig.build()
    await restarted.run_once()
    row = await rig.row()
    assert row.state is RolloverState.ADOPTING
    assert rig.terminal.started[-1] == (
        row.successor_session_id,
        ProjectId("river"),
        rig.journey.destination,
        PermissionPreset.ASK,
    )
    assert len(rig.terminal.started) == 2
    await rig.build().run_once()
    assert len(rig.terminal.started) == 2


@pytest.mark.parametrize(
    "defect,code",
    [
        ("unregistered", "destination-unregistered"),
        ("alias", "ambiguous-destination"),
        ("branch", "branch-mismatch"),
        ("removed", "destination-root"),
    ],
)
async def test_changed_destinations_refuse_without_launch_or_predecessor_stop(routed, defect, code):
    rig = routed
    rig.terminal.idle = False
    await rig.pass_.run_once()
    if defect == "unregistered":
        rig.projects[ProjectId("river")] = rig.journey.repo("replacement")
    elif defect == "alias":
        rig.projects[ProjectId("alias")] = rig.journey.destination
    elif defect == "branch":
        rig.journey.git(rig.journey.destination, "switch", "-q", "-c", "changed")
    else:
        rig.journey.destination.rename(rig.journey.base / "removed-destination")
    rig.terminal.idle = True
    await rig.build().run_once()
    row = await rig.row()
    assert row.state is RolloverState.FAILED
    assert row.failure_code == code
    assert len(rig.terminal.started) == 1
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING


async def test_actual_accepted_digest_and_routing_must_match_frozen_ready(routed):
    import json

    rig = routed
    await rig.pass_.run_once()
    row = await rig.row()
    rig.journey.state(rig.journey.destination, rig.journey.target_plan)
    rig.journey.run("accept", session=str(row.successor_session_id))
    path = rig.journey.envelope("accepted")
    original = path.read_bytes()
    body = json.loads(original)
    body["ready_sha256"] = "f" * 64
    path.write_text(json.dumps(body))
    await rig.pass_.run_once()
    assert (await rig.row()).state is RolloverState.ADOPTING
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    path.write_bytes(original)
    await rig.pass_.run_once()
    assert (await rig.row()).state is RolloverState.COMPLETED
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.ENDED
    assert len(rig.terminal.started) == 2


@pytest.mark.parametrize(
    "defect",
    [
        "version-bool",
        "source",
        "predecessor",
        "plan",
        "relative-root",
        "branch-control",
        "locator",
        "boundary-int",
        "subplan-bool",
        "extra",
        "destination-extra",
        "digest-on-ready",
        "duplicate-key",
        "oversized",
    ],
)
def test_actual_v2_ready_rejects_every_untrusted_schema_variant(journey, defect):
    import json

    journey.run()
    path = journey.envelope()
    body = json.loads(path.read_bytes())
    if defect == "version-bool":
        body["version"] = True
    elif defect == "source":
        body["source_root"] = str(journey.destination)
    elif defect == "predecessor":
        body["predecessor_session_id"] = "wrong-session"
    elif defect == "plan":
        body["plan"] = str(journey.source_plan)
    elif defect == "relative-root":
        body["destination"]["root"] = "relative"
    elif defect == "branch-control":
        body["destination"]["branch"] = "bad\nbranch"
    elif defect == "locator":
        body["destination"]["locator"] = "Task 1.1; bad"
    elif defect == "boundary-int":
        body["selection"]["boundary"] = 1
    elif defect == "subplan-bool":
        body["selection"]["subplan"] = True
    elif defect == "extra":
        body["extra"] = True
    elif defect == "destination-extra":
        body["destination"]["extra"] = True
    elif defect == "digest-on-ready":
        body["ready_sha256"] = "a" * 64
    encoded = json.dumps(body)
    if defect == "duplicate-key":
        encoded = encoded.replace('"version": 2', '"version": 2, "version": 2')
    if defect == "oversized":
        encoded += " " * 4096
    path.write_text(encoded)
    assert FileHandoffEnvelopes().events(journey.source) == ()


async def test_changed_ready_cannot_retarget_a_frozen_transaction(routed):
    import json

    rig = routed
    rig.terminal.idle = False
    await rig.pass_.run_once()
    target = (await rig.row()).target
    third = rig.journey.repo("summit")
    rig.projects[ProjectId("summit")] = third
    path = rig.journey.envelope()
    body = json.loads(path.read_bytes())
    body["destination"]["root"] = str(third)
    path.write_text(json.dumps(body))
    rig.terminal.idle = True
    await rig.build().run_once()
    row = await rig.row()
    assert row.target == target
    assert row.failure_code == "stale-destination"
    assert len(rig.terminal.started) == 1


async def test_failure_after_launch_record_never_relaunches_after_restart(routed, monkeypatch):
    rig = routed

    # The pass has its own store instance on the same connection: fail at its record boundary.
    async def lost(*args, **kwargs):
        raise OSError("synthetic crash after pane launch")

    monkeypatch.setattr(rig.pass_._store, "record_successor", lost)
    await rig.pass_.run_once()
    assert len(rig.terminal.started) == 2
    assert (await rig.row()).state is RolloverState.SUCCESSOR_STARTING
    await rig.build().run_once()
    assert len(rig.terminal.started) == 2
    assert (await rig.row()).failure_code == "successor-unknown"
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING


@pytest.mark.parametrize("removed", [False, True])
@pytest.mark.parametrize("alias", [False, True])
async def test_existing_destination_worktree_keeps_project_and_cleanup_binding(
    journey, removed, alias
):
    from datetime import UTC, datetime

    from remote_agents.ports.worktree_store import Worktree

    rig = RoutedRig(journey)
    try:
        owner = await rig.service.launch(
            LaunchCommand(ProjectId("river"), ProfileId("codex"), "worktree-owner")
        )
        main = journey.destination
        worktree = journey.base / "river-worktree"
        journey.git(main, "worktree", "add", "-q", "-b", "continuation", str(worktree))
        journey.target_plan.write_text(
            journey.target_plan.read_text()
            .replace(str(main), str(worktree))
            .replace("Execution branch: `feature`", "Execution branch: `continuation`")
        )
        journey.source_plan.write_text(
            journey.source_plan.read_text()
            .replace(f"cwd: {main}\n", f"cwd: {worktree}\n")
            .replace("branch: feature\n", "branch: continuation\n")
        )
        journey.destination = worktree
        recorded = worktree
        if alias:
            recorded = journey.base / "worktree-link"
            recorded.symlink_to(worktree, target_is_directory=True)
        await rig.worktrees.record(
            Worktree(
                recorded,
                ProjectId("river"),
                "continuation",
                "feature",
                journey.git(main, "rev-parse", "HEAD"),
                {},
                datetime.now(UTC),
                None,
            ),
            bound_to=owner.record.session_id,
        )
        await rig.start()
        rig.terminal.idle = False
        await rig.pass_.run_once()
        if removed:
            worktree.rename(journey.base / "removed-worktree")
        rig.terminal.idle = True
        await rig.build().run_once()
        row = await rig.row()
        if removed or alias:
            assert row.failure_code == "destination-root"
            assert len(rig.terminal.started) == 2
        else:
            assert row.state is RolloverState.ADOPTING
            bound = await rig.worktrees.folder_for(row.successor_session_id)
            assert bound.path == recorded and bound.project_id == ProjectId("river")
            assert rig.terminal.started[-1][2].resolve() == worktree
            from remote_agents.application.commands import GracefulStopCommand

            await rig.service.graceful_stop(
                GracefulStopCommand(owner.record.session_id, ProfileId("codex"))
            )
            assert recorded not in {w.path for w in await rig.worktrees.removable_candidates()}
    finally:
        rig.connection.close()


async def test_request_channel_does_not_follow_a_later_root_report(routed):
    rig = routed
    ready_path = rig.journey.envelope()
    ready = ready_path.read_bytes()
    ready_path.unlink()
    await rig.rows.request(
        rig.predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        at=(await rig.sessions.get(rig.predecessor)).created_at,
    )
    await rig.pass_.run_once()
    assert (await rig.row()).source_root == rig.journey.source
    third = rig.journey.repo("summit")
    assert rig.reported.record(str(rig.predecessor), third)
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.REQUESTED
    assert (rig.journey.source / ".claude/handoffs/request.json").exists()
    assert not (third / ".claude/handoffs/request.json").exists()
    ready_path.write_bytes(ready)
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.ADOPTING


async def test_legacy_launch_without_a_frozen_destination_holds_both_sessions(routed):
    rig = routed
    # Recreate a pre-upgrade row through the legacy store API, which has no target payload.
    row = await rig.rows.open_for_ready(
        rig.predecessor,
        rig.journey.handoff_id,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        plan=str(rig.journey.target_plan),
        at=(await rig.sessions.get(rig.predecessor)).created_at,
    )
    successor = await rig.service.launch(
        LaunchCommand(ProjectId("river"), ProfileId("codex"), "legacy-successor")
    )
    await rig.rows.advance(row.id, RolloverState.SUCCESSOR_STARTING, at=row.updated_at)
    await rig.rows.record_successor(row.id, successor.record.session_id, at=row.updated_at)
    await rig.rows.advance(row.id, RolloverState.ADOPTING, at=row.updated_at)
    await rig.build().run_once()
    held = await rig.row()
    assert held.state is RolloverState.ADOPTING and held.target is None
    assert any(
        "legacy-destination-unbound" in (e.detail or "") for e in await rig.rows.events(row.id)
    )
    assert len(rig.terminal.started) == 2
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    assert (await rig.sessions.get(successor.record.session_id)).state is SessionState.RUNNING


async def test_cancelled_request_cleanup_uses_frozen_channel_after_root_report_changes(routed):
    rig = routed
    rig.journey.envelope().unlink()
    at = (await rig.sessions.get(rig.predecessor)).created_at
    requested = await rig.rows.request(
        rig.predecessor, project_id=ProjectId("orchard"), profile_id=ProfileId("codex"), at=at
    )
    await rig.pass_.run_once()
    third = rig.journey.repo("summit")
    rig.reported.record(str(rig.predecessor), third)
    await rig.rows.advance(requested.id, RolloverState.CANCELLED, at=at)
    await rig.build().run_once()
    assert not (rig.journey.source / ".claude/handoffs/request.json").exists()


async def test_terminal_state_before_cleanup_is_recovered_from_frozen_channel(routed, monkeypatch):
    rig = routed
    await rig.pass_.run_once()
    row = await rig.row()
    rig.journey.state(rig.journey.destination, rig.journey.target_plan)
    rig.journey.run("accept", session=str(row.successor_session_id))

    class SimulatedCrash(BaseException):
        pass

    async def interrupted(*args):
        raise SimulatedCrash("synthetic interruption before cleanup")

    monkeypatch.setattr(rig.pass_, "_finish", interrupted)
    with pytest.raises(SimulatedCrash, match="synthetic interruption"):
        await rig.pass_.run_once()
    assert (await rig.row()).state is RolloverState.COMPLETED
    assert rig.journey.envelope("accepted").exists()
    await rig.build().run_once()
    assert not rig.journey.envelope("accepted").exists()
    assert not rig.journey.envelope().exists()
    assert len(rig.terminal.started) == 2


async def test_legacy_prelaunch_row_cannot_reinterpret_a_replaced_v2_ready(routed):
    rig = routed
    await rig.rows.open_for_ready(
        rig.predecessor,
        rig.journey.handoff_id,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        plan=str(rig.journey.target_plan),
        at=(await rig.sessions.get(rig.predecessor)).created_at,
    )
    await rig.pass_.run_once()
    assert len(rig.terminal.started) == 1
    assert (await rig.row()).failure_code == "legacy-destination-unbound"
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING


async def test_v1_ready_plan_cannot_change_after_destination_freeze(routed):
    rig = routed
    rig.journey.legacy()
    rig.terminal.idle = False
    await rig.pass_.run_once()
    rig.journey.legacy(plan=rig.journey.records / "replacement-plan.md")
    rig.terminal.idle = True
    await rig.build().run_once()
    assert len(rig.terminal.started) == 1
    assert (await rig.row()).failure_code == "stale-destination"


@pytest.mark.parametrize("outcome", ["accept", "fail"])
async def test_v1_outcome_plan_must_equal_the_frozen_plan(routed, outcome):
    rig = routed
    rig.journey.legacy()
    await rig.pass_.run_once()
    row = await rig.row()
    rig.journey.legacy(
        outcome,
        session=str(row.successor_session_id),
        plan=rig.journey.records / "replacement-plan.md",
    )
    await rig.pass_.run_once()
    assert (await rig.row()).state is RolloverState.ADOPTING
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING


async def test_same_project_canonical_worktree_aliases_are_ambiguous(journey):
    from datetime import UTC, datetime

    from remote_agents.ports.worktree_store import Worktree

    rig = RoutedRig(journey)
    try:
        main = journey.destination
        worktree = journey.base / "river-worktree"
        journey.git(main, "worktree", "add", "-q", "-b", "continuation", str(worktree))
        alias = journey.base / "another-worktree-name"
        alias.symlink_to(worktree, target_is_directory=True)
        for index, path in enumerate((worktree, alias)):
            owner = await rig.service.launch(
                LaunchCommand(ProjectId("river"), ProfileId("codex"), f"prior-{index}")
            )
            await rig.worktrees.record(
                Worktree(
                    path,
                    ProjectId("river"),
                    "continuation",
                    "feature",
                    journey.git(main, "rev-parse", "HEAD"),
                    {},
                    datetime.now(UTC),
                    None,
                ),
                bound_to=owner.record.session_id,
            )
        journey.target_plan.write_text(
            journey.target_plan.read_text()
            .replace(str(main), str(worktree))
            .replace("Execution branch: `feature`", "Execution branch: `continuation`")
        )
        journey.source_plan.write_text(
            journey.source_plan.read_text()
            .replace(f"cwd: {main}\n", f"cwd: {worktree}\n")
            .replace("branch: feature\n", "branch: continuation\n")
        )
        journey.destination = worktree
        await rig.start()
        await rig.pass_.run_once()
        assert (await rig.row()).failure_code == "ambiguous-destination"
        assert len(rig.terminal.started) == 3
    finally:
        rig.connection.close()


async def test_valid_v1_replay_and_legacy_reconstruction_still_complete_once(routed):
    import json
    from datetime import UTC, datetime, timedelta

    rig = routed
    plan = rig.journey.legacy()
    await rig.rows.open_for_ready(
        rig.predecessor,
        rig.journey.handoff_id,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        plan=str(plan),
        at=(await rig.sessions.get(rig.predecessor)).created_at,
    )
    rig.terminal.idle = False
    await rig.pass_.run_once()
    row = await rig.row()
    # The pre-launch pass waits for idle before reconstructing its legacy snapshot.
    rig.terminal.idle = True
    await rig.pass_.run_once()
    row = await rig.row()
    assert row.target.version == 1 and row.source_root == rig.journey.source
    body = json.loads(rig.journey.envelope().read_bytes())
    body["timestamp"] = (datetime.now(UTC) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rig.journey.envelope().write_text(json.dumps(body))
    rig.journey.legacy("verify", plan=plan)
    rig.journey.legacy("accept", session=str(row.successor_session_id), plan=plan)
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.COMPLETED
    assert len(rig.terminal.started) == 2
    assert rig.terminal.started[-1][1:3] == (ProjectId("orchard"), rig.journey.source)


async def test_v2_adoption_prompt_carries_the_explicit_frozen_source_root(routed):
    import shlex

    rig = routed
    await rig.pass_.run_once()
    tokens = shlex.split(rig.terminal.prompts[-1][1])
    assert tokens[tokens.index("--handoff-root") + 1] == str(rig.journey.source)


async def test_real_finished_workflow_answers_only_its_request_without_launch_or_stop(routed):
    import json

    rig = routed
    rig.journey.envelope().unlink()
    await rig.rows.request(
        rig.predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        at=(await rig.sessions.get(rig.predecessor)).created_at,
    )
    await rig.pass_.run_once()
    request = json.loads((rig.journey.source / ".claude/handoffs/request.json").read_text())
    assert request["version"] == 2
    rig.journey.complete_workflow()
    rig.journey.run("finished")
    await rig.build().run_once()
    assert (await rig.row()).state.value == "workflow_finished"
    assert len(rig.terminal.started) == 1
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    assert not (rig.journey.source / ".claude/handoffs/request.json").exists()


async def _workflow_request(rig):
    import json

    rig.journey.envelope().unlink()
    requested = await rig.rows.request(
        rig.predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        at=(await rig.sessions.get(rig.predecessor)).created_at,
    )
    await rig.pass_.run_once()
    request = rig.journey.source / ".claude/handoffs/request.json"
    return requested, request, json.loads(request.read_bytes())


@pytest.mark.parametrize(
    "defect", ["session", "nonce", "time", "root", "version", "outcome", "extra"]
)
async def test_finished_must_correlate_with_the_outstanding_request(routed, defect):
    import json

    rig = routed
    requested, request, original_request = await _workflow_request(rig)
    rig.journey.complete_workflow()
    rig.journey.run("finished")
    path = rig.journey.source / f".claude/handoffs/{original_request['request_id']}.finished.json"
    original = path.read_bytes()
    body = json.loads(original)
    if defect == "session":
        body["managed_session_id"] = "another-managed-session"
    elif defect == "nonce":
        body["request_id"] = "h-" + "f" * 20
    elif defect == "time":
        body["requested_at"] = "2026-01-01T00:00:00Z"
    elif defect == "root":
        body["source_root"] = str(rig.journey.destination)
    elif defect == "version":
        body["version"] = 1
    elif defect == "outcome":
        body["outcome"] = "no-next-work"
    else:
        body["extra"] = True
    path.write_text(json.dumps(body))
    await rig.build().run_once()
    assert (await rig.rows.get(requested.id)).state is RolloverState.REQUESTED
    assert request.exists()
    assert len(rig.terminal.started) == 1
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    path.write_bytes(original)
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.WORKFLOW_FINISHED
    assert not request.exists()


async def test_old_finished_cannot_answer_or_clear_a_newer_request(routed):
    import json

    rig = routed
    first, path, old = await _workflow_request(rig)
    rig.journey.complete_workflow()
    rig.journey.run("finished")
    await rig.rows.advance(first.id, RolloverState.CANCELLED, at=first.requested_at)
    await rig.build().run_once()
    second = await rig.rows.request(
        rig.predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        at=first.requested_at,
    )
    await rig.build().run_once()
    current = json.loads(path.read_bytes())
    assert current["request_id"] != old["request_id"]
    await rig.build().run_once()
    assert (await rig.rows.get(second.id)).state is RolloverState.REQUESTED
    assert json.loads(path.read_bytes()) == current
    # A delayed cleanup from the old process must not unlink the new request.
    await rig.build()._finish(await rig.rows.get(first.id), await rig.build()._look())
    assert json.loads(path.read_bytes()) == current
    assert len(rig.terminal.started) == 1


async def test_no_outstanding_request_cannot_be_completed_by_a_finished_file(routed):
    rig = routed
    rig.journey.envelope().unlink()
    envelopes = FileHandoffEnvelopes()
    envelopes.write_request(rig.journey.source, str(rig.predecessor), request_id="h-" + "f" * 20)
    rig.journey.complete_workflow()
    rig.journey.run("finished")
    envelopes.clear_request(rig.journey.source, str(rig.predecessor), request_id="h-" + "f" * 20)
    await rig.pass_.run_once()
    assert await rig.row() is None
    assert len(rig.terminal.started) == 1
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING


async def test_no_next_subplan_is_not_enough_for_a_finished_workflow(routed):
    rig = routed
    await _workflow_request(rig)
    rig.journey.complete_workflow()
    # Owned state is complete, but the destination has unfinished work.
    rig.journey.target_plan.write_text(rig.journey.target_plan.read_text().replace("[x]", "[ ]"))
    result = rig.journey.run("finished", expected=3)
    assert "workflow-incomplete" in result.stdout + result.stderr
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.REQUESTED
    assert len(rig.terminal.started) == 1


@pytest.mark.parametrize("ending", ["trusted", "declined", "timeout"])
async def test_v2_trust_hold_survives_restart_without_typing_into_the_dialog(
    routed, monkeypatch, ending
):
    from dataclasses import replace
    from datetime import timedelta

    from remote_agents.domain.state_machine import LifecycleEvent

    rig = routed
    launch = rig.terminal.launch

    async def held(*args, **kw):
        return replace(await launch(*args, **kw), awaiting_trust=True)

    monkeypatch.setattr(rig.terminal, "launch", held)
    await rig.pass_.run_once()
    row = await rig.row()
    successor = row.successor_session_id
    assert row.state is RolloverState.SUCCESSOR_STARTING
    assert rig.terminal.prompts == []
    assert rig.terminal.trust_answers == 0
    restarted = rig.build()
    await restarted.run_once()
    assert rig.terminal.prompts == []
    if ending == "timeout":
        restarted._now = lambda: row.updated_at + timedelta(hours=3)
    elif ending == "declined":
        await rig.sessions.record_event(successor, LifecycleEvent.TRUST_DECLINED)
    else:
        await rig.sessions.record_event(successor, LifecycleEvent.READY)
        rig.terminal.idle = False
        await restarted.run_once()
        assert rig.terminal.prompts == []
        rig.terminal.idle = True
    await restarted.run_once()
    row = await rig.row()
    if ending == "trusted":
        import shlex

        assert row.state is RolloverState.ADOPTING
        assert len(rig.terminal.prompts) == 1
        assert shlex.split(rig.terminal.prompts[0][1])[-1] == str(rig.journey.source)
        rig.journey.state(rig.journey.destination, rig.journey.target_plan)
        rig.journey.run("accept", session=str(successor))
        rig.terminal.idle = False
        await restarted.run_once()
        assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
        rig.terminal.idle = True
        await rig.build().run_once()
        assert (await rig.row()).state is RolloverState.COMPLETED
    else:
        assert row.state is RolloverState.FAILED
        assert row.failure_code == (
            "successor-untrusted" if ending == "timeout" else "successor-failed"
        )
        assert rig.terminal.prompts == []
        assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    assert len(rig.terminal.started) == 2


async def test_the_source_root_argument_preserves_spaces_quotes_and_shell_characters(journey):
    import shlex

    # Build a new fixture at its final path, before creating owned state.
    special = journey.base / "source space ' $()"
    special.mkdir()
    nested = WriterJourney(
        special, Path(os.environ["RA_STAGED_PLANNING"]), journey.script.parents[4].name
    )
    rig = RoutedRig(nested)
    try:
        await rig.start()
        await rig.pass_.run_once()
        assert shlex.split(rig.terminal.prompts[-1][1])[-1] == str(nested.source)
    finally:
        rig.connection.close()


async def test_schema24_preserves_populated_legacy_state_history_order_and_constraints(tmp_path):
    import sqlite3
    from datetime import UTC, datetime

    from remote_agents.adapters.sqlite.migrations import MIGRATIONS, apply_migrations
    from remote_agents.domain.models import SessionId

    connection = open_database(tmp_path / "old.sqlite3", migrations=MIGRATIONS[:23])
    predecessor = SessionId.new()
    store = SQLiteRolloverStore(connection)
    row = await store.request(
        predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        at=datetime.now(UTC),
    )
    await store.freeze(row.id, tmp_path, None, at=row.updated_at)
    await store.record_request(row.id, at=row.updated_at)
    before = [tuple(r) for r in connection.execute("SELECT rowid, * FROM rollovers")]
    events = [tuple(r) for r in connection.execute("SELECT * FROM rollover_events")]
    apply_migrations(connection)
    assert [tuple(r) for r in connection.execute("SELECT rowid, * FROM rollovers")] == before
    assert [tuple(r) for r in connection.execute("SELECT * FROM rollover_events")] == events
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE rollover_events SET detail='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE rollovers SET source_root='/changed'")
    ended = await store.advance(row.id, RolloverState.WORKFLOW_FINISHED, at=row.updated_at)
    assert ended.successor_session_id is None
    assert (
        await store.request(
            predecessor, project_id=row.project_id, profile_id=row.profile_id, at=row.updated_at
        )
        is not None
    )
    # No capability snapshot is invented for a launch predating migration24.
    assert await SQLiteSessionStore(connection).handoff_versions(predecessor) == (1,)
    connection.close()


async def test_cleanup_of_a_correlated_request_cannot_unlink_a_different_live_request(routed):
    import json

    rig = routed
    row, path, request = await _workflow_request(rig)
    original = request.copy()
    request["request_id"] = "h-" + "f" * 20
    path.write_text(json.dumps(request))
    await rig.build().run_once()
    assert (await rig.row()).failure_code == "request-lost"
    assert json.loads(path.read_bytes()) == request
    FileHandoffEnvelopes().clear_request(rig.journey.source, str(rig.predecessor))
    assert json.loads(path.read_bytes()) == request
    assert original["request_id"] != request["request_id"]
    assert len(rig.terminal.started) == 1


@pytest.mark.parametrize("race_at", [1, 2])
def test_request_compare_and_remove_preserves_a_concurrent_request_writer(
    tmp_path, monkeypatch, race_at
):
    import concurrent.futures

    from remote_agents.adapters.workflow import handoff_envelopes as wire

    envelopes = FileHandoffEnvelopes()
    first, second = "h-" + "a" * 20, "h-" + "b" * 20
    assert envelopes.write_request(tmp_path, "session", request_id=first)
    original = wire._requested
    attempts = []
    reads = 0

    def racing_read(directory):
        nonlocal reads
        reads += 1
        current = original(directory)
        if reads != race_at:
            return current
        with concurrent.futures.ThreadPoolExecutor() as executor:
            attempts.append(
                executor.submit(
                    envelopes.write_request, tmp_path, "session", request_id=second
                ).result(timeout=3)
            )
        return current

    monkeypatch.setattr(wire, "_requested", racing_read)
    envelopes.clear_request(tmp_path, "session", request_id=first)
    assert attempts == [race_at == 1]
    monkeypatch.setattr(wire, "_requested", original)
    if race_at == 1:
        assert envelopes.request_details(tmp_path).request_id == second
    else:
        assert envelopes.request_details(tmp_path) is None
    assert envelopes.write_request(tmp_path, "session", request_id=second)
    envelopes.clear_request(tmp_path, "session", request_id=first)
    assert envelopes.request_details(tmp_path).request_id == second


async def test_a_matching_finished_response_takes_precedence_over_a_leftover_ready(routed):
    rig = routed
    ready = rig.journey.envelope().read_bytes()
    await _workflow_request(rig)
    rig.journey.complete_workflow()
    rig.journey.run("finished")
    rig.journey.envelope().write_bytes(ready)
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.WORKFLOW_FINISHED
    await rig.build().run_once()
    assert (await rig.row()).state is RolloverState.WORKFLOW_FINISHED
    assert len(rig.terminal.started) == 1
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING


@pytest.mark.parametrize("binding", ["project", "worktree"])
@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("failure", [OSError, RuntimeError])
async def test_destination_canonicalization_failures_are_named_and_preserve_the_predecessor(
    routed, monkeypatch, binding, frozen, failure
):
    from datetime import UTC, datetime

    from remote_agents.ports.worktree_store import Worktree

    rig = routed
    target = rig.journey.destination
    if binding == "worktree":
        rig.projects[ProjectId("river")] = rig.journey.repo("river-main")
        original_live = rig.worktrees.live_for_project
        candidate = Worktree(
            target, ProjectId("river"), "feature", None, "a" * 40, {}, datetime.now(UTC), None
        )

        async def live(project):
            if project == ProjectId("river"):
                return (candidate,)
            return await original_live(project)

        monkeypatch.setattr(rig.worktrees, "live_for_project", live)
    if frozen:
        rig.terminal.idle = False
        await rig.build().run_once()
        assert (await rig.row()).state is RolloverState.HANDOFF_READY
    original_resolve = type(target).resolve

    def unavailable(path, *args, **kwargs):
        if path == target:
            # Python3.12 raises RuntimeError for a symlink loop; 3.13+ suppress it
            # with strict=False. Exercise the port's same failure contract on all hosts.
            raise failure("destination cannot be canonicalized")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "resolve", unavailable)
    rig.terminal.idle = True
    await rig.build().run_once()
    row = await rig.row()
    assert row is not None
    assert row.state is RolloverState.FAILED
    assert row.failure_code == "destination-root"
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    assert len(rig.terminal.started) == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("failure", [OSError, RuntimeError])
async def test_destination_git_inspection_failures_are_named_for_both_wire_versions(
    routed, monkeypatch, legacy, failure
):
    from remote_agents.composition import service

    rig = routed
    if legacy:
        rig.journey.legacy()

    async def unavailable(root):
        raise failure("destination inspection cannot resolve its checkout")

    monkeypatch.setattr(service, "destination_branch", unavailable)
    await rig.build().run_once()
    assert (await rig.row()).failure_code == "destination-root"
    assert (await rig.sessions.get(rig.predecessor)).state is SessionState.RUNNING
    assert len(rig.terminal.started) == 1
