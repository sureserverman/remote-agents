"""A launch's binding: asked before the pane, sent once to an idle composer, never over anything.

The pass runs against the real store, with a stand-in for the terminal. The terminal's own half is
`tests/unit/adapters/tmux/test_binding_line.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from binding_crash import crash_bind

from remote_agents.adapters.agents.hook_identity import RosterEntry, roster_path, write_roster
from remote_agents.adapters.agents.registry import profiles_bound_by_thread, provider_descriptors
from remote_agents.adapters.agents.thread_binding import (
    FileThreadBindings,
    bound_session,
    consume_nonce,
)
from remote_agents.application.thread_binding import (
    COMPOSER_PATIENCE_SECONDS,
    SEEN_PATIENCE_SECONDS,
    ThreadBinder,
)
from remote_agents.composition.backend import _binding_requester
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason
from remote_agents.ports.thread_binding import PendingBinding

_THREAD = "01a193e7-e8c8-7272-9c5f-6ee9e7749464"
_SENT = PromptDelivery(PromptOutcome.SENT)
_COMPOSING = PromptDelivery(PromptOutcome.REFUSED, PromptReason.COMPOSING)


class World:
    def __init__(self, spool: Path) -> None:
        self.spool = spool
        self.bindings = FileThreadBindings(spool)
        self.clock = [datetime(2026, 10, 31, tzinfo=UTC)]
        self.records: list[SessionRecord] = []
        self.sent: list[tuple[SessionId, str]] = []
        self.deliveries: list[PromptDelivery] = []
        self.told: list[SessionId] = []
        self.shell_runs = True
        self.binder = self.new_binder()

    def new_binder(self) -> ThreadBinder:
        async def sessions():
            return list(self.records)

        async def told(record):
            self.told.append(record.session_id)

        async def sleep(seconds):
            self.clock[0] += timedelta(seconds=seconds)

        return ThreadBinder(
            self.bindings,
            sessions=sessions,
            hooks={"codex": "codex"},
            send_line=self.send_line,
            line_for=lambda session, nonce: f"!bind {session} {nonce}",
            notify=told,
            now=lambda: self.clock[0],
            sleep=sleep,
        )

    async def send_line(self, session_id: SessionId, line: str) -> PromptDelivery:
        self.sent.append((session_id, line))
        delivery = self.deliveries.pop(0) if self.deliveries else _SENT
        if delivery is _SENT and self.shell_runs:
            assert consume_nonce(self.spool, str(session_id), line.split()[-1], _THREAD) is None
        return delivery

    def session(
        self,
        *,
        profile: str = "codex",
        state: SessionState = SessionState.RUNNING,
        wanted: bool = True,
        resumed: str | None = None,
    ) -> SessionId:
        session_id = SessionId.new()
        record = SessionRecord(
            session_id,
            ProjectId("p"),
            ProfileId(profile),
            SessionDisplayIdentity("p", profile, "regular", 1),
            state,
            self.clock[0],
            **(
                {"resume_profile_id": ProfileId(profile), "resume_source_id": resumed}
                if resumed
                else {}
            ),
        )
        self.records.append(record)
        write_roster(
            roster_path(self.spool, "codex"),
            [RosterEntry(str(r.session_id), "/w", r.created_at) for r in self.records],
        )
        if wanted:
            self.bindings.request(str(session_id))
        return session_id

    def run(self, passes: int = 1) -> None:
        for _ in range(passes):
            asyncio.run(self.binder.run_once())

    def elapse(self, seconds: float) -> None:
        self.clock[0] += timedelta(seconds=seconds)


@pytest.fixture
def world(tmp_path: Path) -> World:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    return World(spool)


def test_a_launch_that_asked_is_sent_one_line_and_binds(world: World) -> None:
    session = world.session()
    world.run(3)
    assert [sent[0] for sent in world.sent] == [session]
    assert bound_session(world.spool, _THREAD) == str(session)
    assert world.told == []


def test_a_session_that_never_asked_is_never_typed_into(world: World) -> None:
    """A session already running when this version is installed included."""
    world.session(wanted=False)
    world.run(3)
    world.elapse(COMPOSER_PATIENCE_SECONDS * 2)
    world.run()
    assert world.sent == []
    assert world.told == []


def test_only_a_provider_with_a_thread_variable_asks(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    bound = profiles_bound_by_thread()
    assert bound == {"codex": "codex"}
    request = _binding_requester(provider_descriptors(), spool)
    codex, claude = SessionId.new(), SessionId.new()
    request(codex, ProfileId("codex"))
    request(claude, ProfileId("claude"))
    bindings = FileThreadBindings(spool)
    assert bindings.state(str(codex)) is PendingBinding.WANTED
    assert bindings.state(str(claude)) is PendingBinding.NONE


@pytest.mark.parametrize("profile", ["claude", "opencode", "cursor"])
def test_another_provider_gets_no_line_even_with_a_file_standing(
    world: World, profile: str
) -> None:
    world.session(profile=profile)
    world.run(2)
    assert world.sent == []


@pytest.mark.parametrize(
    "state", [SessionState.UNTRUSTED, SessionState.STARTING, SessionState.STOP_REQUESTED]
)
def test_a_session_not_running_is_not_typed_into(world: World, state: SessionState) -> None:
    world.session(state=state)
    world.run(2)
    assert world.sent == []


def test_a_resumed_session_needs_no_line(world: World) -> None:
    world.session(resumed=_THREAD)
    world.run(2)
    assert world.sent == []


def test_a_composer_holding_text_waits_then_tells_the_owner_once(world: World) -> None:
    session = world.session()
    world.deliveries = [_COMPOSING] * 3
    world.run(2)
    assert len(world.sent) == 2
    world.elapse(COMPOSER_PATIENCE_SECONDS)
    world.run(3)
    assert world.told == [session]
    assert world.bindings.state(str(session)) is PendingBinding.GAVE_UP


def test_a_refused_line_keeps_one_nonce_until_it_lands(world: World) -> None:
    world.session()
    world.deliveries = [_COMPOSING]
    world.run(2)
    assert world.sent[0][1] == world.sent[1][1]


def test_a_line_that_may_have_landed_is_never_sent_twice(world: World) -> None:
    session = world.session()
    world.deliveries = [PromptDelivery(PromptOutcome.UNCONFIRMED, PromptReason.SUBMIT_NOT_SEEN)]
    world.run(3)
    assert len(world.sent) == 1
    world.elapse(SEEN_PATIENCE_SECONDS)
    world.run()
    assert len(world.sent) == 1
    assert world.told == [session]


def test_a_send_that_raises_is_treated_as_may_have_landed(world: World) -> None:
    world.session()
    calls = []

    async def raising(session_id, line):
        calls.append(line)
        raise RuntimeError("tmux went away")

    world.send_line = raising  # type: ignore[method-assign]
    world.binder = world.new_binder()
    world.run(3)
    assert len(calls) == 1


def test_a_line_sent_with_no_binding_seen_tells_the_owner_once(world: World) -> None:
    session = world.session()
    world.shell_runs = False
    world.run()
    world.elapse(SEEN_PATIENCE_SECONDS)
    world.run(3)
    assert world.told == [session]


def test_a_restart_retries_once_with_a_fresh_nonce_and_the_old_one_is_dead(world: World) -> None:
    session = world.session()
    world.shell_runs = False
    world.run()
    old = world.sent[0][1].split()[-1]

    world.binder = world.new_binder()
    world.run()
    assert len(world.sent) == 2
    assert world.sent[1][1].split()[-1] != old
    assert consume_nonce(world.spool, str(session), old, _THREAD) is not None
    assert world.bindings.state(str(session)) is PendingBinding.RETRIED

    world.binder = world.new_binder()
    world.run(2)
    assert len(world.sent) == 2, "no third try"
    assert world.told == [session]


def test_a_restart_never_types_to_a_session_already_bound(world: World) -> None:
    world.session()
    world.run()
    world.binder = world.new_binder()
    world.run(2)
    assert len(world.sent) == 1


def test_a_restart_does_not_tell_again_about_one_already_given_up(world: World) -> None:
    session = world.session()
    world.shell_runs = False
    world.run()
    world.elapse(SEEN_PATIENCE_SECONDS)
    world.run()
    assert world.told == [session]
    world.binder = world.new_binder()
    world.run(2)
    assert world.told == [session]
    assert len(world.sent) == 1


def test_a_store_that_cannot_be_trusted_types_nothing_and_tells_once(world: World) -> None:
    session = world.session()
    (world.spool / "thread-binding").chmod(0o750)
    world.run(2)
    (world.spool / "thread-binding").chmod(0o700)
    assert world.sent == []
    assert world.told == [session]


def test_a_session_that_gave_up_as_a_successor_leaves_the_notice_to_its_rollover(
    world: World,
) -> None:
    session = world.session()
    world.shell_runs = False

    async def successor(session_id):
        return session_id == session

    world.binder._told_elsewhere = successor
    world.run()
    world.elapse(SEEN_PATIENCE_SECONDS)
    world.run(2)
    assert world.told == []


def test_a_session_nothing_is_pending_for_costs_no_binding_read(world: World) -> None:
    world.session(wanted=False)
    calls = []
    names = world.bindings.names
    world.bindings.names = lambda *args: (calls.append(args), names(*args))[1]  # type: ignore[method-assign]
    world.run(2)
    assert calls == []


def test_wait_bound_says_yes_for_a_provider_that_needs_no_binding(world: World) -> None:
    assert asyncio.run(world.binder.wait_bound(SessionId.new(), ProfileId("claude"))) is True


def test_wait_bound_gives_up_without_telling_the_owner_itself(world: World) -> None:
    session = world.session()
    world.shell_runs = False
    assert asyncio.run(world.binder.wait_bound(session, ProfileId("codex"))) is False
    assert world.told == []
    assert len(world.sent) == 1


def test_a_mark_that_vetoes_the_binding_is_not_called_bound(world: World) -> None:
    from remote_agents.adapters.agents.turn_markers import FileConversationMarks

    session = world.session()
    FileConversationMarks(world.spool).record(str(session), "01a193e8-0e07-7bc0-a39e-1b6af05448f0")
    assert asyncio.run(world.binder.wait_bound(session, ProfileId("codex"))) is False


async def test_a_launch_asks_before_its_pane_starts_and_a_resume_never_does(tmp_path: Path) -> None:
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
    from remote_agents.adapters.tmux.fake import FakeTerminal
    from remote_agents.application.commands import LaunchCommand
    from remote_agents.application.services import SessionService

    class Recording(FakeTerminal):
        async def launch(self, session_id, *args, **kwargs):
            order.append("pane")
            return await super().launch(session_id, *args, **kwargs)

    order: list[str] = []
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")),
        Recording(),
        request_binding=lambda session_id, profile_id: order.append(f"asked {profile_id}"),
    )
    await service.launch(LaunchCommand(ProjectId("p"), ProfileId("codex"), "regular"))
    assert order == ["asked codex", "pane"]


async def test_a_binding_that_cannot_be_asked_costs_the_binding_never_the_launch(
    tmp_path: Path,
) -> None:
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
    from remote_agents.adapters.tmux.fake import FakeTerminal
    from remote_agents.application.commands import LaunchCommand
    from remote_agents.application.services import SessionService

    def refuse(session_id, profile_id):
        raise OSError("the store cannot be written")

    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")),
        FakeTerminal(),
        request_binding=refuse,
    )
    outcome = await service.launch(LaunchCommand(ProjectId("p"), ProfileId("codex"), "regular"))
    assert outcome.record.state is SessionState.RUNNING


@pytest.mark.parametrize("phase", ["claim", "publish"])
def test_a_crashed_claim_is_retried_once_by_the_restarted_service(world: World, phase: str) -> None:
    session = world.session()
    old = world.bindings.mint(str(session))
    crash_bind(world.spool, str(session), old, _THREAD, phase)
    world.binder = world.new_binder()
    world.run(3)
    assert len(world.sent) == 1
    assert world.sent[0][1].split()[-1] != old
    assert world.bindings.names(str(session), "codex")
    assert consume_nonce(world.spool, str(session), old, _THREAD) is not None
    world.binder = world.new_binder()
    world.run(2)
    assert len(world.sent) == 1
    assert world.told == []


@pytest.mark.parametrize("phase", ["claim", "publish"])
def test_a_crashed_retry_gives_up_visibly_without_a_third_try(world: World, phase: str) -> None:
    session = world.session()
    nonce = world.bindings.mint(str(session), retry=True)
    crash_bind(world.spool, str(session), nonce, _THREAD, phase)
    world.binder = world.new_binder()
    world.run(3)
    assert world.sent == []
    assert world.told == [session]
    assert world.bindings.state(str(session)) is PendingBinding.GAVE_UP
    world.binder = world.new_binder()
    world.run(2)
    assert world.sent == []
    assert world.told == [session]
    assert consume_nonce(world.spool, str(session), nonce, _THREAD) is not None


def test_a_crash_after_publication_never_sends_another_binding_line(world: World) -> None:
    session = world.session()
    nonce = world.bindings.mint(str(session))
    crash_bind(world.spool, str(session), nonce, _THREAD, "published")
    world.binder = world.new_binder()
    world.run(3)
    assert world.bindings.names(str(session), "codex")
    assert world.sent == []
    assert world.told == []


@pytest.mark.parametrize("unavailable", ["roster", "own_mark"])
def test_a_published_claim_never_revokes_a_binding_when_names_is_temporarily_false(
    world: World, unavailable: str
) -> None:
    from remote_agents.adapters.agents.turn_markers import FileConversationMarks

    session = world.session()
    nonce = world.bindings.mint(str(session))
    crash_bind(world.spool, str(session), nonce, _THREAD, "published")
    if unavailable == "roster":
        roster_path(world.spool, "codex").unlink()
    else:
        FileConversationMarks(world.spool).record(str(session), "another-thread")
    assert not world.bindings.names(str(session), "codex"), "the refusal is the required control"
    world.shell_runs = False
    world.binder = world.new_binder()
    world.run(3)
    assert world.sent == [], "publication committed: never type a replacement binding line"
    assert bound_session(world.spool, _THREAD) == str(session)
    assert world.told == []
