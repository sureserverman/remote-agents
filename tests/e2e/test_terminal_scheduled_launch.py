"""A due schedule, fired on a real tmux server: the session starts and its message lands once.

A harmless fake agent draws a one-line composer (a rule, then `❯ `) and echoes each line it is
given as `GOT: <line>`. The fire pass runs over the real SQLite stores, the real
`SessionService` and the real `TmuxTerminal`, so the launch, the guarded send's idle check, the
paste, the `Enter` and the submit check are all the production ones. Run alone: real-tmux tests
fail spuriously beside another uv/pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.limit_stop_store import SQLiteLimitStopStore
from remote_agents.adapters.sqlite.schedule_store import SQLiteScheduleStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.schedules import (
    FireOutcome,
    FireReport,
    SchedulePass,
    limit_stopped_in,
    still_working_in,
)
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor
from remote_agents.ports.schedules import Repeat, Schedule

PROJECT = ProjectId("opaque-editor")
PROFILE = ProfileId("fake")
MESSAGE = "reply with OK"

#: The agent draws its composer this long after its readiness marker -- which is when the launch
#: returns -- so the first tries of the message meet a screen with no composer on it yet.
BOOT_DELAY = 1.0

_AGENT = f"""
import time
print("READY", flush=True)
time.sleep({BOOT_DELAY})
while True:
    print("─" * 24, flush=True)
    try:
        line = input("❯ ")
    except EOFError:
        break
    print("GOT: " + line, flush=True)
"""

_COMPOSER = ComposerScreen(composer=r"^─{10,}\n❯ ?(?P<draft>[^\n]*)\Z")


class _NoMarkers:
    def started_at(self, session_id: str):
        return None


async def test_a_due_schedule_launches_a_session_and_its_message_lands_once(
    tmp_path: Path,
) -> None:
    agent = tmp_path / "fake_agent.py"
    agent.write_text(_AGENT, encoding="utf-8")
    socket = f"remote-agents-test-{uuid4().hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "i")
    terminal = TmuxTerminal(
        gateway,
        {PROJECT: tmp_path},
        {
            PROFILE: LaunchProfile(
                sys.executable, (sys.executable, str(agent)), {"PATH": os.environ["PATH"]}, "READY"
            )
        },
        startup_timeout=10.0,
        composers={"fake": ProviderDescriptor(PROFILE, "F", composer=_COMPOSER)},
    )
    connection = open_database(tmp_path / "sessions.sqlite3")
    sessions = SQLiteSessionStore(connection)
    schedules = SQLiteScheduleStore(connection)
    now = datetime.now(UTC)
    await schedules.add(
        Schedule(
            id="s1",
            project_id=PROJECT,
            profile_id=PROFILE,
            prompt=MESSAGE,
            # Half a day from now, so the second tick can never find the next time due.
            when=Repeat.daily((now + timedelta(hours=12)).time()),
            paused=False,
            next_fire_at=now - timedelta(seconds=1),
            created_at=now - timedelta(days=1),
        )
    )
    reports: list[FireReport] = []

    async def notify(report: FireReport) -> None:
        reports.append(report)

    sends: list[str] = []

    async def counted_send(session_id, text):
        sends.append(text)
        return await terminal.send_prompt(session_id, text)

    fire_pass = SchedulePass(
        schedules,
        launch=SessionService(sessions, terminal).launch,
        send=counted_send,
        limit_stopped=limit_stopped_in(sessions, SQLiteLimitStopStore(connection)),
        working=still_working_in(sessions, _NoMarkers()),
        notify=notify,
        zone=lambda: UTC,
    )
    launched: list[SessionId] = []
    try:
        (report,) = await fire_pass.run()
        assert report.session_id is not None
        launched.append(SessionId.parse(report.session_id))

        assert report.outcome is FireOutcome.STARTED, report
        screen = ""
        for _ in range(40):
            screen = await terminal.capture(launched[0])
            if f"GOT: {MESSAGE}" in screen:
                break
            await asyncio.sleep(0.1)
        assert f"GOT: {MESSAGE}" in screen, screen
        assert len(sends) >= 2, "the first try met the booting screen and was tried again"
        await asyncio.sleep(0.5)
        screen = await terminal.capture(launched[0])
        assert screen.count(f"GOT: {MESSAGE}") == 1, "typed once"
        assert reports == [report]

        # The next tick finds nothing due: the schedule moved to tomorrow, and the one session
        # it started is the only one there is.
        assert await fire_pass.run() == ()
        advanced = await schedules.get("s1")
        assert advanced is not None and advanced.next_fire_at > now
        assert advanced.last_session_id == report.session_id
        assert len(await sessions.list()) == 1
        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == launched
    finally:
        for session_id in launched:
            try:
                await gateway.destroy(session_id)
            except RuntimeError:
                pass
        # And the server itself, whatever a failed launch left on it.
        killing = await asyncio.create_subprocess_exec(
            "tmux", "-L", socket, "kill-server", stderr=asyncio.subprocess.DEVNULL
        )
        await killing.wait()
        connection.close()
