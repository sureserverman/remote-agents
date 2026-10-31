"""A schedule fire never makes a worktree, even on a contended project (DEC-114, BL-116).

Schedules launch regular: the mode is the owner's choice when offered, and nobody is there to
make it at fire time. Driven through the real `SessionService`, with a worktree service that
would offer the mode and records any creation, so a fire that asked for one is seen.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import timedelta
from pathlib import Path

from schedule_fakes import MemoryScheduleStore

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.schedules import FireOutcome, SchedulePass
from remote_agents.application.services import SessionService
from remote_agents.ports.terminal import TerminalObservation

from .test_schedule_pass import BERLIN, FIRE, Rig, _schedule


class _Contended:
    """Another session is live, so the mode would be offered; any creation is recorded."""

    def __init__(self) -> None:
        self.created: list[object] = []

    async def offered(self, project_id) -> bool:
        return True

    async def create(self, session_id, project_id):
        self.created.append(session_id)
        raise AssertionError("a schedule fire made a worktree")


class _Terminal:
    async def launch(self, session_id, project_id, profile_id, *, remote_control):
        return TerminalObservation(session_id, True, False)


async def test_a_fire_on_a_contended_project_launches_regular_and_makes_no_worktree(
    tmp_path: Path,
) -> None:
    worktrees = _Contended()
    sessions = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")),
        _Terminal(),  # type: ignore[arg-type]
        worktree_service=worktrees,  # type: ignore[arg-type]
    )
    assert await sessions.worktree_offered(_schedule().project_id)
    launched: list[LaunchCommand] = []

    async def launch(command: LaunchCommand):
        launched.append(command)
        return await sessions.launch(command)

    rig = Rig()
    rig.clock[:] = [FIRE + timedelta(seconds=20)]
    fire_pass = SchedulePass(
        MemoryScheduleStore(_schedule()),
        launch=launch,
        send=rig.send,
        limit_stopped=rig.limit_stopped,
        working=rig.is_working,
        notify=rig.notify,
        zone=lambda: BERLIN,
        now=lambda: rig.clock[0],
        sleep=rig.sleep,
    )

    reports = await fire_pass.run()

    assert [report.outcome for report in reports] == [FireOutcome.STARTED]
    assert [command.mode for command in launched] == ["regular"]
    assert worktrees.created == []


def test_the_fire_names_its_mode_rather_than_inheriting_a_default() -> None:
    """DEC-114 stated in the code: a later default change must not reach schedules silently."""
    source = textwrap.dedent(inspect.getsource(SchedulePass._launch_for))
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "LaunchCommand"
    ]
    assert len(calls) == 1
    modes = [keyword.value for keyword in calls[0].keywords if keyword.arg == "mode"]
    assert len(modes) == 1 and isinstance(modes[0], ast.Constant) and modes[0].value == "regular"
