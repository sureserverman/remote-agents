"""Actual built writer -> five-project composed application -> durable restart.

Host/synthetic terminal evidence, explicitly separate from native acceptance.
"""

import json
import os
from pathlib import Path

import pytest
from five_project_journey import FiveProjectWriter, JourneyRig

from remote_agents.domain.models import ProjectId, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.rollover import RolloverState


@pytest.fixture
def writer(tmp_path, request):
    checkout = os.environ.get("RA_STAGED_PLANNING")
    if not checkout:
        pytest.fail("BLOCKED: RA_STAGED_PLANNING must name the qualified built writer")
    return FiveProjectWriter(tmp_path, Path(checkout), request.param)


@pytest.mark.parametrize("writer", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("restart", ["before_launch", "after_record", "after_acceptance"])
async def test_five_project_master_preserves_destination_preset_and_exactly_once(writer, restart):
    rig = JourneyRig(writer, writer.host)
    try:
        predecessor = await rig.start()
        preset = PermissionPreset.APPROVE_FOR_ME if str(rig.profile) == "codex" else None
        hops = [(0, 1, 1, 1), (1, 1, 1, 2), (1, 2, 2, 1), (2, 1, 3, 1), (3, 1, 4, 1)]
        for source_index, source_stage, target_index, target_stage in hops:
            source = writer.targets[source_index]
            target = writer.targets[target_index]
            source_plan, target_plan = writer.plans[source_index], writer.plans[target_index]
            locator = f"Task {target_stage}.1"
            writer.completed.add((source_index, source_stage))
            writer.render()
            # First boundary is owner-requested; the later boundaries are automatic.
            if source_index == 0:
                await rig.rows.request(
                    predecessor.session_id,
                    project_id=predecessor.project_id,
                    profile_id=rig.profile,
                    at=predecessor.created_at,
                )
                await rig.pass_.run_once()
                assert (source / ".claude/handoffs/request.json").is_file()
            writer.prepare(source, source_plan, target, target_plan, locator)
            writer.invoke(
                "ready",
                source,
                source_plan,
                predecessor.session_id,
                boundary=source_index != target_index,
                locator=locator,
            )
            before = len(rig.terminal.started)
            rig.terminal.idle = False
            await rig.pass_.run_once()
            row = await rig.rows.latest_for(predecessor.session_id)
            assert row.target.destination.root == target
            assert row.target.destination.plan == str(target_plan)
            assert row.target.destination.locator == locator
            assert row.target.permissions is preset
            assert row.target.destination.branch == writer.git(target, "branch", "--show-current")
            assert row.reason == ("owner" if source_index == 0 else "workflow")
            await rig.settings.set(PermissionPreset.FULL_ACCESS)
            if restart == "before_launch":
                rig.reopen()
            rig.terminal.idle = True
            await rig.pass_.run_once()
            row = await rig.rows.latest_for(predecessor.session_id)
            assert row.state is RolloverState.ADOPTING
            assert len(rig.terminal.started) == before + 1
            successor = await rig.sessions.get(row.successor_session_id)
            assert successor.project_id == ProjectId(chr(97 + target_index))
            assert rig.terminal.started[-1] == (
                successor.session_id,
                successor.project_id,
                target,
                preset,
            )
            assert (await rig.sessions.get(predecessor.session_id)).state is SessionState.RUNNING
            if restart == "after_record":
                rig.reopen()
                await rig.pass_.run_once()
                assert len(rig.terminal.started) == before + 1
            writer.ensure_state(target, target_plan)
            writer.invoke("verify", target, target_plan, successor.session_id, source=source)
            writer.resume_owned_state(target, target_plan, target_stage)
            writer.invoke("accept", target, target_plan, successor.session_id, source=source)
            accepted_path = source / f".claude/handoffs/{writer.handoff_id}.accepted.json"
            assert json.loads(accepted_path.read_text())["ready_sha256"]
            if restart == "after_acceptance":

                class Interrupted(BaseException):
                    pass

                advance = rig.pass_._store.advance

                async def interrupted_advance(*args, **kwargs):
                    row = await advance(*args, **kwargs)
                    if row.state is RolloverState.SUCCESSOR_ACCEPTED:
                        raise Interrupted
                    return row

                rig.pass_._store.advance = interrupted_advance
                with pytest.raises(Interrupted):
                    await rig.pass_.run_once()
                assert (
                    await rig.rows.latest_for(predecessor.session_id)
                ).state is RolloverState.SUCCESSOR_ACCEPTED
                assert (
                    await rig.sessions.get(predecessor.session_id)
                ).state is SessionState.RUNNING
                rig.reopen()
            await rig.pass_.run_once()
            row = await rig.rows.latest_for(predecessor.session_id)
            assert row.state is RolloverState.COMPLETED
            assert (await rig.sessions.get(predecessor.session_id)).state is SessionState.ENDED
            assert rig.terminal.stopped.count(predecessor.session_id) == 1
            await rig.pass_.run_once()
            assert len(rig.terminal.started) == before + 1
            assert rig.terminal.stopped.count(predecessor.session_id) == 1
            predecessor = successor
        assert predecessor.project_id == ProjectId("e")
        assert len(rig.terminal.started) == 7  # Existing D owner, A, five successors.
        assert len(rig.terminal.stopped) == 5
    finally:
        rig.connection.close()


@pytest.mark.parametrize("writer", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize(
    "defect,code",
    [
        ("removed", "destination-root"),
        ("branch", "branch-mismatch"),
        ("registry", "destination-unregistered"),
        ("alias", "ambiguous-destination"),
    ],
)
async def test_unavailable_journey_target_refuses_and_preserves_the_predecessor(
    writer, defect, code
):
    rig = JourneyRig(writer, writer.host)
    try:
        predecessor = await rig.start()
        writer.completed.add((0, 1))
        writer.render()
        source, target = writer.roots[:2]
        source_plan, target_plan = writer.plans[:2]
        writer.prepare(source, source_plan, target, target_plan, "Task 1.1")
        writer.invoke("ready", source, source_plan, predecessor.session_id, boundary=True)
        rig.terminal.idle = False
        await rig.pass_.run_once()
        before = len(rig.terminal.started)
        if defect == "removed":
            target.rename(writer.base / "removed-B")
        elif defect == "branch":
            writer.git(target, "switch", "-q", "-c", "changed")
        elif defect == "registry":
            rig.projects[ProjectId("b")] = writer.roots[2]
        else:
            rig.projects[ProjectId("alias")] = target
        rig.reopen()
        rig.terminal.idle = True
        await rig.pass_.run_once()
        row = await rig.rows.latest_for(predecessor.session_id)
        assert row.state is RolloverState.FAILED
        assert row.failure_code == code
        assert (await rig.sessions.get(predecessor.session_id)) == predecessor
        assert len(rig.terminal.started) == before
        assert not rig.terminal.stopped
    finally:
        rig.connection.close()


@pytest.mark.parametrize("writer", ["codex", "claude"], indirect=True)
async def test_interruption_before_successor_record_holds_without_a_second_launch(writer):
    class Interrupted(BaseException):
        pass

    rig = JourneyRig(writer, writer.host)
    try:
        predecessor = await rig.start()
        writer.completed.add((0, 1))
        writer.render()
        source, target = writer.roots[:2]
        source_plan, target_plan = writer.plans[:2]
        writer.prepare(source, source_plan, target, target_plan, "Task 1.1")
        writer.invoke("ready", source, source_plan, predecessor.session_id, boundary=True)
        before = len(rig.terminal.started)

        async def crash_before_record(*_args, **_kwargs):
            raise Interrupted

        rig.pass_._store.record_successor = crash_before_record
        with pytest.raises(Interrupted):
            await rig.pass_.run_once()
        assert len(rig.terminal.started) == before + 1
        row = await rig.rows.latest_for(predecessor.session_id)
        assert row.state is RolloverState.SUCCESSOR_STARTING
        assert row.successor_session_id is None
        rig.reopen()
        for _ in range(3):
            await rig.pass_.run_once()
        row = await rig.rows.latest_for(predecessor.session_id)
        assert row.failure_code == "successor-unknown"
        assert (await rig.sessions.get(predecessor.session_id)) == predecessor
        assert len(rig.terminal.started) == before + 1
        assert not rig.terminal.stopped
    finally:
        rig.connection.close()
