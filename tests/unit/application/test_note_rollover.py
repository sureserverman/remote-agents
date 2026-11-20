"""Rollover with a note (BL-119, OD-2): the pass asks an idle agent for a note, then waits for it.

The service types one fixed request holding its own path (`NOTE_REQUEST`), once, into an idle
composer; no byte of it comes from the session. The note counts once it is a regular file of
this user's, not a link, 1 byte to 32 KiB, and the agent is idle again. With no note within
`NOTE_PATIENCE` of the asking, the rollover fails `note-unwritten` and no session is started.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.workflow.handoff_envelopes import FileHandoffEnvelopes
from remote_agents.adapters.workflow.handoff_notes import MAX_NOTE_BYTES, FileHandoffNotes
from remote_agents.application.commands import GracefulStopCommand, LaunchCommand
from remote_agents.application.rollover import (
    ADOPTION_PATIENCE,
    NOT_IDLE_PATIENCE,
    NOTE_FIRST_MESSAGE,
    NOTE_PATIENCE,
    NOTE_REFUSED,
    NOTE_REQUEST,
    NOTE_UNWRITTEN,
    PREDECESSOR_NOT_IDLE,
    RolloverPass,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverKind, RolloverState
from remote_agents.ports.rollover_store import Rollover
from remote_agents.ports.terminal import (
    PromptDelivery,
    PromptOutcome,
    PromptReason,
    TerminalObservation,
)

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
PROJECT = ProjectId("tv-set")
CLAUDE = ProfileId("claude")
CODEX = ProfileId("codex")
SENT = PromptDelivery(PromptOutcome.SENT)


def _record(session_id: SessionId, profile=CLAUDE, label: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_id,
        PROJECT,
        profile,
        SessionDisplayIdentity("tv-set", str(profile), "regular", 1, label),
        SessionState.RUNNING,
        NOW,
    )


@dataclass
class NoteRig:
    """A real store, a real handoff folder and fakes for the terminal, recording each act."""

    store: SQLiteRolloverStore
    root: Path
    predecessor: SessionId = field(default_factory=SessionId.new)
    sessions: dict[SessionId, SessionRecord] = field(default_factory=dict)
    switch: bool = False
    not_idle: set[SessionId] = field(default_factory=set)
    sends: list[tuple[SessionId, str]] = field(default_factory=list)
    deliveries: list[PromptDelivery] = field(default_factory=list)
    launches: list[LaunchCommand] = field(default_factory=list)
    stops: list[GracefulStopCommand] = field(default_factory=list)
    reports: list = field(default_factory=list)
    clock: list[datetime] = field(default_factory=lambda: [NOW])
    profile: ProfileId = CLAUDE
    label: str | None = None
    _pass: RolloverPass | None = None

    def __post_init__(self) -> None:
        self.sessions[self.predecessor] = _record(self.predecessor, self.profile, self.label)

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(self.sessions.values())

    async def enabled(self) -> bool:
        return self.switch

    async def idle(self, session_id: SessionId) -> bool:
        return session_id not in self.not_idle

    async def launch(self, command: LaunchCommand) -> object:
        self.launches.append(command)
        raise AssertionError("Task 2.3 starts no session")

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        self.sends.append((session_id, text))
        return self.deliveries.pop(0) if self.deliveries else SENT

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.stops.append(command)
        raise AssertionError("Task 2.3 stops no session")

    async def told(self, report) -> None:
        self.reports.append(report)

    def later(self, delta: timedelta) -> None:
        self.clock[0] += delta

    def pass_(self) -> RolloverPass:
        if self._pass is None:
            self._pass = self.new_pass()
        return self._pass

    def new_pass(self) -> RolloverPass:
        templates = adopt_templates()
        return RolloverPass(
            self.store,
            FileHandoffEnvelopes(),
            adopt_templates={CLAUDE: templates[CLAUDE], CODEX: templates[CODEX]},
            note_rollable=frozenset({CLAUDE, CODEX}),
            notes=FileHandoffNotes(),
            sessions=self.list_sessions,
            handoff_root=lambda project: self.root if project == PROJECT else None,
            enabled=self.enabled,
            idle=self.idle,
            launch=self.launch,
            send=self.send,
            graceful_stop=self.graceful_stop,
            notify=self.told,
            now=lambda: self.clock[0],
        )

    async def run(self, passes: int = 1) -> None:
        for _ in range(passes):
            await self.pass_().run_once()

    async def press(self) -> Rollover:
        asked = await self.store.request(
            self.predecessor,
            project_id=PROJECT,
            profile_id=self.profile,
            at=self.clock[0],
            kind=RolloverKind.NOTE,
        )
        assert asked is not None
        return asked

    async def row(self) -> Rollover:
        (row,) = self.store._select("1 = 1", ())
        return row

    def note_folder(self, rollover: Rollover) -> Path:
        """The rollover's own folder (OD-13): outside `.claude/`, which Claude asks before
        writing into (OD-12), and inside the session's folder, which Codex's sandbox writes."""
        return self.root / f".remote-agents-note-{rollover.id}"

    def note_path(self, rollover: Rollover) -> Path:
        return self.note_folder(rollover) / "note.md"

    def expected_request(self, rollover: Rollover) -> str:
        return NOTE_REQUEST.format(path=self.note_path(rollover))


@pytest.fixture
def rig(tmp_path: Path):
    root = tmp_path / "tv-set"
    root.mkdir()
    rig = NoteRig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")), root)
    yield rig
    assert rig.launches == [] and rig.stops == []


async def test_the_pass_types_the_fixed_request_with_its_own_path_once(rig: NoteRig) -> None:
    asked = await rig.press()

    await rig.run(passes=3)

    assert rig.sends == [(rig.predecessor, rig.expected_request(asked))]
    assert (await rig.row()).state is RolloverState.REQUESTED
    folder = rig.note_folder(asked)
    assert folder.is_dir()
    assert (folder / ".gitignore").read_text() == "*\n"
    assert not (rig.root / ".claude").exists(), "nothing is made under .claude/"


@pytest.mark.parametrize(
    "label",
    ["release prep", "x`; rm -rf ~ #", "/exit Write something else", "{path}"],
)
async def test_no_byte_of_the_typed_request_comes_from_the_session(
    tmp_path: Path, label: str
) -> None:
    root = tmp_path / "tv-set"
    root.mkdir()
    rig = NoteRig(
        SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")), root, label=label
    )
    asked = await rig.press()

    await rig.run()

    ((_, typed),) = rig.sends
    assert typed.encode() == rig.expected_request(asked).encode()
    assert label not in typed
    assert str(rig.note_path(asked)) in typed


async def test_the_request_does_not_need_the_auto_rollover_switch(rig: NoteRig) -> None:
    rig.switch = False
    await rig.press()

    await rig.run()

    assert len(rig.sends) == 1


async def test_a_codex_session_is_asked_the_same_way(tmp_path: Path) -> None:
    root = tmp_path / "tv-set"
    root.mkdir()
    rig = NoteRig(
        SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")), root, profile=CODEX
    )
    asked = await rig.press()

    await rig.run()

    assert rig.sends == [(rig.predecessor, rig.expected_request(asked))]


async def test_a_session_that_is_not_idle_is_typed_nothing_and_fails_after_the_patience(
    rig: NoteRig,
) -> None:
    """Busy, composing, on a dialog or on a screen nothing recognises: `idle` says no."""
    rig.not_idle.add(rig.predecessor)
    await rig.press()

    await rig.run(passes=3)
    assert rig.sends == []
    assert (await rig.row()).state is RolloverState.REQUESTED

    rig.later(NOT_IDLE_PATIENCE)
    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == PREDECESSOR_NOT_IDLE
    assert rig.sends == []


async def test_the_request_is_typed_once_across_a_restart(rig: NoteRig) -> None:
    await rig.press()
    await rig.run()
    assert len(rig.sends) == 1

    rig._pass = rig.new_pass()  # the service restarted
    await rig.run(passes=2)

    assert len(rig.sends) == 1


async def test_a_send_that_was_refused_is_never_retried(rig: NoteRig) -> None:
    rig.deliveries.append(PromptDelivery(PromptOutcome.REFUSED, PromptReason.COMPOSING))
    await rig.press()

    await rig.run(passes=3)

    assert len(rig.sends) == 1
    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == "not-typed"


def _write(path: Path, body: bytes) -> None:
    path.write_bytes(body)


async def test_a_written_note_and_an_idle_agent_make_the_rollover_handoff_ready(
    rig: NoteRig,
) -> None:
    asked = await rig.press()
    await rig.run()
    rig.not_idle.add(rig.predecessor)  # still writing
    _write(rig.note_path(asked), b"# Handoff\nDoing X; next Y.\n")

    await rig.run()
    assert (await rig.row()).state is RolloverState.REQUESTED, "not idle yet"

    rig.not_idle.clear()
    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.HANDOFF_READY
    assert row.kind is RolloverKind.NOTE and row.source_root == rig.root


def _link(path: Path) -> None:
    target = path.parent.parent / "elsewhere.md"
    target.write_bytes(b"a note somewhere else\n")
    os.symlink(target, path)


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(_link, id="a link"),
        pytest.param(lambda path: _write(path, b""), id="empty"),
        pytest.param(lambda path: _write(path, b"x" * (MAX_NOTE_BYTES + 1)), id="over 32 KiB"),
        pytest.param(lambda path: path.mkdir(), id="a directory"),
    ],
)
async def test_a_note_that_is_not_a_small_regular_file_is_not_accepted(rig: NoteRig, plant) -> None:
    asked = await rig.press()
    await rig.run()
    plant(rig.note_path(asked))

    await rig.run(passes=2)
    assert (await rig.row()).state is RolloverState.REQUESTED

    rig.later(NOTE_PATIENCE)
    await rig.run()
    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == NOTE_REFUSED


async def test_another_users_note_is_not_accepted(rig: NoteRig, monkeypatch) -> None:
    asked = await rig.press()
    await rig.run()
    _write(rig.note_path(asked), b"planted\n")
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)

    await rig.run()

    assert (await rig.row()).state is RolloverState.REQUESTED


async def test_no_note_within_the_patience_fails_once_and_starts_nothing(rig: NoteRig) -> None:
    await rig.press()
    await rig.run()
    rig.later(NOTE_PATIENCE - timedelta(seconds=1))
    await rig.run()
    assert (await rig.row()).state is RolloverState.REQUESTED

    rig.later(timedelta(seconds=1))
    await rig.run(passes=2)

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == NOTE_UNWRITTEN
    assert len(rig.reports) == 1, "one push says so"
    assert len(rig.sends) == 1


async def test_the_patience_counts_from_the_asking_not_the_press(rig: NoteRig) -> None:
    rig.not_idle.add(rig.predecessor)
    await rig.press()
    await rig.run()
    rig.later(NOT_IDLE_PATIENCE - timedelta(seconds=1))
    rig.not_idle.clear()
    await rig.run()
    assert len(rig.sends) == 1

    rig.later(NOTE_PATIENCE - timedelta(seconds=1))
    await rig.run()

    assert (await rig.row()).state is RolloverState.REQUESTED


async def test_a_cancel_before_the_typing_types_nothing(rig: NoteRig) -> None:
    from remote_agents.application.rollover_book import RolloverBook

    rig.not_idle.add(rig.predecessor)
    await rig.press()
    await rig.run()
    assert await RolloverBook(rig.store).cancel(rig.predecessor)

    rig.not_idle.clear()
    await rig.run(passes=2)

    assert rig.sends == []
    assert (await rig.row()).state is RolloverState.CANCELLED


async def test_a_note_rollover_leaves_the_workflow_request_alone(rig: NoteRig) -> None:
    """The note rollover writes no `request.json`: that file is the workflow's channel."""
    rig.switch = True
    await rig.press()

    await rig.run(passes=2)

    assert not (rig.root / ".claude" / "handoffs" / "request.json").exists()


async def test_the_note_file_is_removed_when_the_rollover_ends(rig: NoteRig) -> None:
    asked = await rig.press()
    await rig.run()
    _write(rig.note_path(asked), b"half a note\n")
    rig.not_idle.add(rig.predecessor)
    rig.later(NOTE_PATIENCE)

    await rig.run()

    assert (await rig.row()).state is RolloverState.FAILED
    assert not rig.note_folder(asked).exists()


async def test_a_profile_not_declaring_note_rollover_fails_not_rollable(tmp_path: Path) -> None:
    root = tmp_path / "tv-set"
    root.mkdir()
    rig = NoteRig(
        SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")),
        root,
        profile=ProfileId("opencode"),
    )
    await rig.press()

    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == "not-rollable"
    assert rig.sends == []


# Tier-1 review of Task 2.3 --------------------------------------------------------------


async def test_the_asking_is_recorded_only_on_a_row_still_requested(rig: NoteRig) -> None:
    """A cancel that lands between the pass's read and its record refuses the record, so
    nothing is typed (the store's conditional note, not a second read)."""
    from remote_agents.ports.rollover_store import NOTE_ASKED, IllegalRolloverMove

    asked = await rig.press()
    await rig.store.advance(asked.id, RolloverState.CANCELLED, at=rig.clock[0])

    with pytest.raises(IllegalRolloverMove):
        await rig.store.note(
            asked.id, NOTE_ASKED, at=rig.clock[0], expected_state=RolloverState.REQUESTED
        )
    assert NOTE_ASKED not in {e.detail for e in await rig.store.events(asked.id)}


async def test_a_cancelled_note_rollover_of_a_note_only_profile_loses_its_note(
    tmp_path: Path,
) -> None:
    """A profile that may roll over with a note but writes no workflow handoffs still has its
    note removed once the owner cancels."""
    from remote_agents.application.rollover_book import RolloverBook

    root = tmp_path / "tv-set"
    root.mkdir()
    rig = NoteRig(
        SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")), root, profile=CODEX
    )
    templates = adopt_templates()

    def note_only_pass() -> RolloverPass:
        return RolloverPass(
            rig.store,
            FileHandoffEnvelopes(),
            adopt_templates={CLAUDE: templates[CLAUDE]},  # Codex writes no workflow handoffs
            note_rollable=frozenset({CLAUDE, CODEX}),
            notes=FileHandoffNotes(),
            sessions=rig.list_sessions,
            handoff_root=lambda project: rig.root if project == PROJECT else None,
            enabled=rig.enabled,
            idle=rig.idle,
            launch=rig.launch,
            send=rig.send,
            graceful_stop=rig.graceful_stop,
            notify=rig.told,
            now=lambda: rig.clock[0],
        )

    rig.new_pass = note_only_pass  # type: ignore[method-assign]
    asked = await rig.press()
    await rig.run()
    assert len(rig.sends) == 1
    _write(rig.note_path(asked), b"a note\n")
    rig.not_idle.add(rig.predecessor)
    await rig.run()
    assert await RolloverBook(rig.store).cancel(rig.predecessor)

    await rig.run()

    assert not rig.note_folder(asked).exists()


@pytest.mark.parametrize("name", ["tv`set", "tv\x1bset", "tv\nset"])
async def test_a_root_whose_name_cannot_be_typed_safely_is_refused(
    tmp_path: Path, name: str
) -> None:
    root = tmp_path / name
    root.mkdir()
    rig = NoteRig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")), root)
    await rig.press()

    await rig.run()

    assert rig.sends == []
    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == "no-handoff-root"


async def test_a_note_written_while_the_session_stays_busy_fails_as_not_idle(
    rig: NoteRig,
) -> None:
    """Not "wrote no note": the note is there, the session never went idle again."""
    asked = await rig.press()
    await rig.run()
    _write(rig.note_path(asked), b"a note\n")
    rig.not_idle.add(rig.predecessor)
    rig.later(NOTE_PATIENCE)

    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == PREDECESSOR_NOT_IDLE


# Task 2.4 -- the successor reads the note; the old session stops after its first turn ------


@dataclass
class SuccessorRig(NoteRig):
    """`NoteRig` whose launch starts a successor and whose feed records finished turns."""

    launched_state: SessionState = SessionState.RUNNING
    turns: dict[SessionId, list[datetime]] = field(default_factory=dict)

    async def launch(self, command: LaunchCommand) -> object:
        self.launches.append(command)
        record = replace(_record(SessionId.new(), command.profile_id), state=self.launched_state)
        self.sessions[record.session_id] = record
        return _Launched(record)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.stops.append(command)
        self.sessions[command.session_id] = replace(
            self.sessions[command.session_id], state=SessionState.ENDED
        )
        return TerminalObservation(command.session_id, False, True)

    async def finished_turn(self, session_id: SessionId, since: datetime) -> bool:
        return any(at >= since for at in self.turns.get(session_id, ()))

    def turn(self, session_id: SessionId) -> None:
        self.turns.setdefault(session_id, []).append(self.clock[0])

    @property
    def successor(self) -> SessionId:
        (other,) = [s for s in self.sessions if s != self.predecessor]
        return other

    def new_pass(self) -> RolloverPass:
        templates = adopt_templates()
        return RolloverPass(
            self.store,
            FileHandoffEnvelopes(),
            adopt_templates={CLAUDE: templates[CLAUDE], CODEX: templates[CODEX]},
            note_rollable=frozenset({CLAUDE, CODEX}),
            notes=FileHandoffNotes(),
            finished_turn=self.finished_turn,
            sessions=self.list_sessions,
            handoff_root=lambda project: self.root if project == PROJECT else None,
            enabled=self.enabled,
            idle=self.idle,
            launch=self.launch,
            send=self.send,
            graceful_stop=self.graceful_stop,
            notify=self.told,
            now=lambda: self.clock[0],
        )

    async def to_handoff_ready(self) -> Rollover:
        asked = await self.press()
        await self.run()
        _write(self.note_path(asked), b"# Handoff\nDoing X; next Y.\n")
        await self.run()
        assert (await self.row()).state is RolloverState.HANDOFF_READY
        return asked


@dataclass(frozen=True)
class _Launched:
    record: SessionRecord
    remote_control: bool = False


@pytest.fixture
def successor_rig(tmp_path: Path):
    root = tmp_path / "tv-set"
    root.mkdir()
    rig = SuccessorRig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")), root)
    yield rig
    assert all(command.only_if_idle for command in rig.stops)


async def test_the_successor_is_typed_only_the_fixed_first_message(
    successor_rig: SuccessorRig,
) -> None:
    rig = successor_rig
    asked = await rig.to_handoff_ready()

    await rig.run()

    (command,) = rig.launches
    assert (command.project_id, command.profile_id) == (PROJECT, CLAUDE)
    assert command.reuse_folder_of == rig.predecessor
    assert command.inherit_permissions_of == rig.predecessor
    assert command.idempotency_key == f"rollover:{asked.id}"
    first = NOTE_FIRST_MESSAGE.format(path=rig.note_path(asked))
    assert rig.sends[-1] == (rig.successor, first)
    assert (await rig.row()).state is RolloverState.ADOPTING
    assert rig.stops == []


async def test_the_predecessor_stops_only_after_the_successor_finishes_a_turn(
    successor_rig: SuccessorRig,
) -> None:
    rig = successor_rig
    asked = await rig.to_handoff_ready()
    await rig.run()
    await rig.run(passes=2)
    assert rig.stops == []

    rig.later(timedelta(minutes=1))
    rig.turn(rig.successor)
    await rig.run()

    assert rig.stops == [GracefulStopCommand(rig.predecessor, CLAUDE, only_if_idle=True)]
    row = await rig.row()
    assert row.state is RolloverState.COMPLETED
    assert await rig.store.continued_as(rig.predecessor) == rig.successor
    assert not rig.note_folder(asked).exists()


async def test_a_turn_finished_before_the_first_message_does_not_count(
    successor_rig: SuccessorRig,
) -> None:
    """A successor that finished a turn of its own before it was typed to has not read the
    note: only a turn observed after the typing adopts it."""
    rig = successor_rig
    await rig.to_handoff_ready()
    rig.later(timedelta(seconds=-5))

    async def early_turn(command: LaunchCommand) -> object:
        launched = await SuccessorRig.launch(rig, command)
        rig.turn(launched.record.session_id)  # recorded before the typing
        rig.later(timedelta(seconds=5))
        return launched

    rig.launch = early_turn  # type: ignore[method-assign]
    rig._pass = rig.new_pass()
    await rig.run(passes=3)

    assert rig.stops == []
    assert (await rig.row()).state is RolloverState.ADOPTING


async def test_no_finished_turn_within_the_patience_fails_and_keeps_the_predecessor(
    successor_rig: SuccessorRig,
) -> None:
    rig = successor_rig
    asked = await rig.to_handoff_ready()
    await rig.run()

    rig.later(ADOPTION_PATIENCE)
    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == "adoption-timeout"
    assert rig.sessions[rig.predecessor].state is SessionState.RUNNING
    assert rig.stops == []
    assert not rig.note_folder(asked).exists()


async def test_an_untrusted_successor_holds_and_is_typed_once_trusted(
    successor_rig: SuccessorRig,
) -> None:
    rig = successor_rig
    rig.launched_state = SessionState.UNTRUSTED
    asked = await rig.to_handoff_ready()
    sends_before = len(rig.sends)

    await rig.run(passes=2)
    assert len(rig.sends) == sends_before, "nothing typed into the trust dialog"
    assert (await rig.row()).state is RolloverState.SUCCESSOR_STARTING

    rig.sessions[rig.successor] = replace(rig.sessions[rig.successor], state=SessionState.RUNNING)
    await rig.run(passes=2)

    first = NOTE_FIRST_MESSAGE.format(path=rig.note_path(asked))
    assert rig.sends[sends_before:] == [(rig.successor, first)]
    assert (await rig.row()).state is RolloverState.ADOPTING


async def test_the_note_is_removed_when_the_rollover_is_cancelled(
    successor_rig: SuccessorRig,
) -> None:
    from remote_agents.application.rollover_book import RolloverBook

    rig = successor_rig
    asked = await rig.press()
    await rig.run()
    _write(rig.note_path(asked), b"a note\n")
    rig.not_idle.add(rig.predecessor)  # not yet idle again: stays REQUESTED
    await rig.run()
    assert await RolloverBook(rig.store).cancel(rig.predecessor)

    await rig.run()

    assert (await rig.row()).state is RolloverState.CANCELLED
    assert not rig.note_folder(asked).exists()
    assert rig.launches == []


async def test_the_note_rollover_runs_under_a_destination_resolver(
    successor_rig: SuccessorRig,
) -> None:
    """Production passes a destination resolver; a note row has no frozen target and must
    not be held as an unbound legacy destination."""
    rig = successor_rig

    async def refuse(*_args):
        raise AssertionError("a note rollover resolves no destination")

    base = rig.new_pass

    def resolved_pass() -> RolloverPass:
        made = base()
        made._resolve_destination = refuse
        made._resolve_legacy_destination = refuse
        return made

    rig.new_pass = resolved_pass  # type: ignore[method-assign]
    await rig.to_handoff_ready()
    await rig.run()
    rig.later(timedelta(minutes=1))
    rig.turn(rig.successor)
    await rig.run()

    assert (await rig.row()).state is RolloverState.COMPLETED


async def test_an_adopting_note_rollover_nothing_was_typed_into_fails_at_once(
    successor_rig: SuccessorRig,
) -> None:
    """A restart between the launch and the first message leaves ADOPTING with no
    NOTE_FIRST_TYPED: that absence proves nothing was typed, so the successor never read the
    note, and waiting out the adoption's patience would only delay saying so."""
    rig = successor_rig
    asked = await rig.to_handoff_ready()
    for state in (RolloverState.SUCCESSOR_STARTING,):
        await rig.store.advance(asked.id, state, at=rig.clock[0])
    successor = SessionId.new()
    rig.sessions[successor] = _record(successor)
    await rig.store.record_successor(asked.id, successor, at=rig.clock[0])
    await rig.store.advance(asked.id, RolloverState.ADOPTING, at=rig.clock[0])

    rig._pass = rig.new_pass()  # the service restarted
    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == "not-typed"
    assert rig.stops == []
    assert rig.sends[-1][0] == rig.predecessor, "nothing was typed into the successor"


# Task 2.6 -- the note's own folder (OD-12, OD-13) -------------------------------------


async def test_a_link_at_the_note_folders_name_is_refused_and_nothing_is_typed(
    rig: NoteRig, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    asked = await rig.press()
    os.symlink(elsewhere, rig.note_folder(asked))

    await rig.run()

    row = await rig.row()
    assert row.state is RolloverState.FAILED and row.failure_code == "no-handoff-root"
    assert rig.sends == []
    assert list(elsewhere.iterdir()) == [], "nothing was written through the link"
    assert rig.note_folder(asked).is_symlink(), "the link is left, not followed or removed"


async def test_a_folder_the_agent_left_more_in_is_emptied_of_ours_only(rig: NoteRig) -> None:
    """Only the names the service made or asked for go; anything else the agent wrote there is
    the owner's to look at, so the folder stays with it."""
    asked = await rig.press()
    await rig.run()
    _write(rig.note_path(asked), b"a note\n")
    _write(rig.note_folder(asked) / "scratch.txt", b"the agent's own\n")
    rig.not_idle.add(rig.predecessor)
    rig.later(NOTE_PATIENCE)

    await rig.run()

    assert (await rig.row()).state is RolloverState.FAILED
    assert sorted(p.name for p in rig.note_folder(asked).iterdir()) == [".gitignore", "scratch.txt"]


async def test_a_note_written_after_the_rollover_ended_is_removed_on_a_later_pass(
    rig: NoteRig,
) -> None:
    """A failed or cancelled note rollover keeps its session running, and the agent may still be
    writing: a note it writes after the clean-up is removed again by a later pass, so no folder
    stays in the project without its `.gitignore`."""
    asked = await rig.press()
    await rig.run()
    rig.later(NOTE_PATIENCE)
    await rig.run()
    assert (await rig.row()).failure_code == NOTE_UNWRITTEN
    assert not rig.note_folder(asked).exists()

    rig.note_folder(asked).mkdir()  # the agent's late Write makes the parent itself
    _write(rig.note_path(asked), b"a late note\n")
    await rig.run()

    assert not rig.note_folder(asked).exists()
