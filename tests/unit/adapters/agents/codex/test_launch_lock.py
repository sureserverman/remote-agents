"""A Codex launch and a Codex Remote Control change never overlap (BL-037).

Every change of the Remote Control preference restarts Codex's daemon, and a Codex TUI still in
its daemon handshake exits when that happens (`docs/acceptance-2026-10-codex-enable-race.md`).
So both sides take one cross-process lock: a launch until its handshake is drawn, a change for
its whole run. The bot's service and the local surface both launch and both toggle, so the
holder in these cases is often a real second process.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex.launch_lock import CodexLaunchLock
from remote_agents.application.commands import (
    LAUNCH_WAIT_SECONDS,
    AnswerTrustCommand,
    LaunchCommand,
)
from remote_agents.application.host_remote_control import (
    HostRemoteControlCommand,
    HostRemoteControlService,
)
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)
from remote_agents.domain.state_machine import transition
from remote_agents.domain.trust import TrustState
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy
from remote_agents.ports.terminal import TerminalObservation, TrustAnswer

CODEX = ProfileId("codex")
CLAUDE = ProfileId("claude")
PROJECT = ProjectId("project")

_HOLDER = textwrap.dedent(
    """
    import asyncio, sys, time
    from pathlib import Path
    from remote_agents.adapters.agents.codex.launch_lock import CodexLaunchLock

    async def main(path, ready, seconds, alone):
        async with CodexLaunchLock(Path(path)).hold(5, alone=alone == "change"):
            Path(ready).write_text("held")
            time.sleep(float(seconds))

    asyncio.run(main(*sys.argv[1:]))
    """
)


class _HeldElsewhere:
    """Another process holding the lock, the way the other surface's toggle (`change`) or
    launch (`launch`) would."""

    def __init__(self, path: Path, seconds: float, by: str = "change") -> None:
        self._ready = path.with_suffix(".ready")
        self._process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(path), str(self._ready), str(seconds), by]
        )
        deadline = time.monotonic() + 15
        while not self._ready.exists():
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(0.02)

    def __enter__(self) -> _HeldElsewhere:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._process.wait(timeout=30)


class Store:
    def __init__(self) -> None:
        self.records: dict[SessionId, SessionRecord] = {}
        self.claims: set[str] = set()

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        return 1

    async def save(self, record: SessionRecord) -> None:
        self.records[record.session_id] = record

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.records.get(session_id)

    async def record_event(self, session_id: SessionId, event) -> SessionRecord:
        current = self.records[session_id]
        updated = replace(current, state=transition(current.state, event).to_state)
        self.records[session_id] = updated
        return updated

    async def claim_idempotency_key(self, key: str) -> bool:
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


class Terminal:
    """A launch that can be held open, as a Codex launch is held open by its handshake."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def launch(self, session_id, project_id, profile_id, *, remote_control=False):
        self.events.append(f"launch:{profile_id}")
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        self.events.append(f"launched:{profile_id}")
        return TerminalObservation(session_id, live=True, preserved=False)

    async def answer_trust(self, session_id: SessionId) -> TrustAnswer:
        self.events.append("answer_trust")
        return TrustAnswer(pressed=True, observed=TrustState.UNKNOWN)


class Control:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def set_state(self, desired: RemoteControlState) -> HostRemoteControlStatus:
        self.events.append(f"set_state:{desired.value}")
        return HostRemoteControlStatus.observed(HostConnection.CONNECTED, server_name=None)


def _sessions(store: Store, terminal: Terminal, lock: CodexLaunchLock) -> SessionService:
    return SessionService(store, terminal, locks=SessionLocks(), daemon_guards={CODEX: lock})


@pytest.mark.parametrize("desired", [RemoteControlState.ACTIVE, RemoteControlState.INACTIVE])
async def test_an_enable_waits_for_a_codex_launch_in_flight(
    tmp_path: Path, desired: RemoteControlState
) -> None:
    """Both directions restart the daemon, so both wait until the launch has finished starting."""
    lock = CodexLaunchLock(tmp_path / "codex-launch.lock")
    terminal = Terminal()
    terminal.release = asyncio.Event()
    sessions = _sessions(Store(), terminal, lock)
    toggles = HostRemoteControlService(
        Control(terminal.events), Store(), SessionLocks(), guard=lock
    )

    launching = asyncio.create_task(sessions.launch(LaunchCommand(PROJECT, CODEX, "launch-1")))
    await terminal.entered.wait()
    toggling = asyncio.create_task(toggles.set_state(HostRemoteControlCommand(desired, "t-1")))
    await asyncio.sleep(0.3)
    assert terminal.events == ["launch:codex"], "the change ran while the launch was starting"

    terminal.release.set()
    await asyncio.gather(launching, toggling)
    assert terminal.events == ["launch:codex", "launched:codex", f"set_state:{desired.value}"]


async def test_an_enable_waits_for_a_launch_in_the_other_process(tmp_path: Path) -> None:
    path = tmp_path / "codex-launch.lock"
    events: list[str] = []
    toggles = HostRemoteControlService(
        Control(events), Store(), SessionLocks(), guard=CodexLaunchLock(path)
    )
    with _HeldElsewhere(path, seconds=1.0, by="launch"):
        started = time.monotonic()
        await toggles.set_state(HostRemoteControlCommand(RemoteControlState.ACTIVE, "t-1"))
        waited = time.monotonic() - started
    assert events == ["set_state:active"]
    assert waited > 0.5, f"the change did not wait for the other process ({waited:.2f}s)"


async def test_a_codex_launch_is_refused_by_name_while_an_enable_holds_the_lock(
    tmp_path: Path,
) -> None:
    """Refused after its two seconds, with nothing claimed, saved or launched."""
    path = tmp_path / "codex-launch.lock"
    store = Store()
    terminal = Terminal()
    sessions = _sessions(store, terminal, CodexLaunchLock(path))
    with _HeldElsewhere(path, seconds=LAUNCH_WAIT_SECONDS + 3):
        started = time.monotonic()
        with pytest.raises(CodexRemoteControlBusy):
            await sessions.launch(LaunchCommand(PROJECT, CODEX, "launch-1"))
        waited = time.monotonic() - started
    assert waited < LAUNCH_WAIT_SECONDS + 0.5, f"the launch waited {waited:.2f}s"
    assert terminal.events == []
    assert store.records == {} and store.claims == set(), "a refused launch left a trace"


async def test_a_non_codex_launch_never_takes_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "codex-launch.lock"
    terminal = Terminal()
    sessions = _sessions(Store(), terminal, CodexLaunchLock(path))
    with _HeldElsewhere(path, seconds=3.0):
        started = time.monotonic()
        await sessions.launch(LaunchCommand(PROJECT, CLAUDE, "launch-1"))
        waited = time.monotonic() - started
    assert terminal.events == ["launch:claude", "launched:claude"]
    assert waited < 0.5, f"a claude launch waited {waited:.2f}s on Codex's lock"


async def test_an_unattended_launch_waits_out_a_change(tmp_path: Path) -> None:
    """A schedule or rollover launch is given longer than an owner's, so it is not spent."""
    path = tmp_path / "codex-launch.lock"
    terminal = Terminal()
    sessions = _sessions(Store(), terminal, CodexLaunchLock(path))
    with _HeldElsewhere(path, seconds=LAUNCH_WAIT_SECONDS + 1):
        await sessions.launch(LaunchCommand(PROJECT, CODEX, "sched-1", guard_wait=30.0))
    assert terminal.events == ["launch:codex", "launched:codex"]


async def test_a_trust_answer_is_refused_before_its_key_is_claimed(tmp_path: Path) -> None:
    """Answering starts the handshake, so it holds the lock too; refused, it spends nothing."""
    path = tmp_path / "codex-launch.lock"
    store = Store()
    session_id = SessionId.new()
    store.records[session_id] = SessionRecord(
        session_id,
        PROJECT,
        CODEX,
        SessionDisplayIdentity("project", "codex", "regular", 1),
        SessionState.UNTRUSTED,
        datetime.now(UTC),
    )
    terminal = Terminal()
    sessions = _sessions(store, terminal, CodexLaunchLock(path))
    with _HeldElsewhere(path, seconds=LAUNCH_WAIT_SECONDS + 3):
        with pytest.raises(CodexRemoteControlBusy):
            await sessions.answer_trust(AnswerTrustCommand(session_id, "trust-1"))
    assert terminal.events == [] and store.claims == set()

    await sessions.answer_trust(AnswerTrustCommand(session_id, "trust-1"))
    assert terminal.events == ["answer_trust"]


async def test_a_change_waits_at_most_its_bound_and_runs_nothing(tmp_path: Path) -> None:
    lock = CodexLaunchLock(tmp_path / "codex-launch.lock")
    events: list[str] = []
    with pytest.raises(CodexRemoteControlBusy):
        async with lock.hold(5, alone=False):
            async with CodexLaunchLock(tmp_path / "codex-launch.lock").hold(0.2, alone=True):
                events.append("ran")
    assert events == []


async def test_without_a_lock_file_the_lock_still_serialises_this_process() -> None:
    order: list[str] = []

    async def holder(name: str) -> None:
        async with CodexLaunchLock(None).hold(5, alone=True):
            for step in range(3):
                order.append(f"{name}{step}")
                await asyncio.sleep(0.01)

    await asyncio.gather(holder("a"), holder("b"))
    assert order in (["a0", "a1", "a2", "b0", "b1", "b2"], ["b0", "b1", "b2", "a0", "a1", "a2"])


async def test_codex_launches_do_not_wait_for_each_other(tmp_path: Path) -> None:
    """Launches race a change, not each other: a slow launch elsewhere holds nobody up, and a
    second launch is not refused with a reason that names a change nobody made."""
    path = tmp_path / "codex-launch.lock"
    terminal = Terminal()
    sessions = _sessions(Store(), terminal, CodexLaunchLock(path))
    with _HeldElsewhere(path, seconds=3.0, by="launch"):
        started = time.monotonic()
        await sessions.launch(LaunchCommand(PROJECT, CODEX, "launch-1"))
        waited = time.monotonic() - started
    assert terminal.events == ["launch:codex", "launched:codex"]
    assert waited < 0.5, f"a Codex launch waited {waited:.2f}s on another launch"


async def test_two_launches_in_one_process_start_together(tmp_path: Path) -> None:
    terminal = Terminal()
    terminal.release = asyncio.Event()
    sessions = _sessions(Store(), terminal, CodexLaunchLock(tmp_path / "codex-launch.lock"))
    first = asyncio.create_task(sessions.launch(LaunchCommand(PROJECT, CODEX, "launch-1")))
    second = asyncio.create_task(sessions.launch(LaunchCommand(PROJECT, CODEX, "launch-2")))
    await asyncio.sleep(0.3)
    assert terminal.events == ["launch:codex", "launch:codex"], terminal.events
    terminal.release.set()
    await asyncio.gather(first, second)


async def test_a_resume_is_refused_by_name_while_a_change_holds_the_lock(tmp_path: Path) -> None:
    """A resume starts a Codex TUI like a launch, so it is guarded like one."""
    from types import SimpleNamespace

    from remote_agents.application.commands import ResumeCommand

    path = tmp_path / "codex-launch.lock"
    store = Store()
    sessions = _sessions(store, Terminal(), CodexLaunchLock(path))
    # Only the profile is read before the guard; the rest of the conversation never is.
    conversation = SimpleNamespace(summary=SimpleNamespace(profile_id=CODEX))
    command = ResumeCommand(PROJECT, CODEX, conversation, "resume-1")
    with _HeldElsewhere(path, seconds=LAUNCH_WAIT_SECONDS + 3):
        with pytest.raises(CodexRemoteControlBusy):
            await sessions.resume(command)
    assert store.records == {} and store.claims == set()


async def test_a_launch_that_fails_releases_the_lock(tmp_path: Path) -> None:
    lock = CodexLaunchLock(tmp_path / "codex-launch.lock")

    class Failing(Terminal):
        async def launch(self, *arguments, **keywords):
            raise RuntimeError("tmux refused")

    sessions = _sessions(Store(), Failing(), lock)
    with pytest.raises(RuntimeError):
        await sessions.launch(LaunchCommand(PROJECT, CODEX, "launch-1"))
    async with lock.hold(0.2, alone=True):
        pass
