"""Declining trust, end to end through the real service and the real store.

DEC-078 is the entry this file exists to keep honest: it lets one button end a session with no
confirmation, on the argument that an `UNTRUSTED` session holds no work. That argument is only
safe while the refusal it depends on actually holds, so the refusal is asserted here beside the
happy path rather than trusted to the matrix.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.commands import DeclineTrustCommand, LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.state_machine import InvalidTransition
from remote_agents.ports.terminal import NOT_AWAITING_TRUST, TerminalObservation


class _Terminal:
    """Records which route a decline took, because the two are the whole of the behaviour."""

    def __init__(self, *, awaiting_trust: bool = True) -> None:
        self.awaiting_trust = awaiting_trust
        self.declined: list[SessionId] = []

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        del project_id, profile_id
        return TerminalObservation(
            session_id, live=True, preserved=False, awaiting_trust=self.awaiting_trust
        )

    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        del profile_id
        return TerminalObservation(
            session_id, live=True, preserved=False, awaiting_trust=self.awaiting_trust
        )

    #: Set when the pane has moved on since the record was written -- the stale-record race.
    still_asking = True

    async def decline_trust(self, session_id: SessionId) -> TerminalObservation:
        if not self.still_asking:
            return TerminalObservation(
                session_id, live=True, preserved=False, detail=NOT_AWAITING_TRUST
            )
        self.declined.append(session_id)
        return TerminalObservation(session_id, live=False, preserved=False)


def _service(tmp_path, terminal: _Terminal) -> SessionService:
    return SessionService(SQLiteSessionStore(open_database(tmp_path / "s.sqlite3")), terminal)


async def _untrusted(service: SessionService, profile: str, key: str):
    return await service.launch(LaunchCommand(ProjectId("opaque-editor"), ProfileId(profile), key))


async def test_declining_ends_an_untrusted_claude_session(tmp_path) -> None:
    terminal = _Terminal()
    service = _service(tmp_path, terminal)
    record = await _untrusted(service, "claude", "decline-claude")
    assert record.state is SessionState.UNTRUSTED

    ended = await service.decline_trust(
        DeclineTrustCommand(record.session_id, "decline-claude-press")
    )

    assert ended.state is SessionState.ENDED
    events = await service._store.events(record.session_id)
    assert [event.event_type for event in events] == ["trust_required", "trust_declined"]
    assert terminal.declined == [record.session_id]


async def test_declining_a_codex_session_still_ends_it(tmp_path) -> None:
    """The profile whose dialog this project will not type into still gets the *no*.

    codex is not in TRUST_ANSWERABLE, so the terminal reaches it by killing the pane rather
    than by answering the question. The lifecycle does not care which route was taken — the
    session ended because the owner said no — and that is the whole reason the decline is a
    lifecycle event rather than a keystroke.
    """
    terminal = _Terminal()
    service = _service(tmp_path, terminal)
    record = await _untrusted(service, "codex", "decline-codex")

    ended = await service.decline_trust(
        DeclineTrustCommand(record.session_id, "decline-codex-press")
    )

    assert ended.state is SessionState.ENDED
    events = await service._store.events(record.session_id)
    assert [event.event_type for event in events] == ["trust_required", "trust_declined"]


async def test_declining_is_refused_for_a_session_that_is_actually_running(tmp_path) -> None:
    """DEC-078's bound, asserted. The unconfirmed kill exists only where there is no work.

    If this refusal ever stops holding, DEC-078's whole safety argument goes with it — an
    unconfirmed button would reach a session with an agent working in it.
    """
    terminal = _Terminal(awaiting_trust=False)
    service = _service(tmp_path, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "running-one")
    )
    assert record.state is SessionState.RUNNING

    with pytest.raises(InvalidTransition):
        await service.decline_trust(DeclineTrustCommand(record.session_id, "decline-running-press"))

    assert terminal.declined == []
    assert (await service._store.get(record.session_id)).state is SessionState.RUNNING


async def test_a_replayed_decline_press_declines_nothing_a_second_time(tmp_path) -> None:
    """DEC-011's property, delivered by the matrix rather than by the token.

    The refusal is `InvalidTransition`, not `DuplicateCommandError`, and the order is
    deliberate: the state is checked before the key is claimed, so the second press is
    refused for the true reason — there is no longer an untrusted session to decline —
    rather than for a bookkeeping one. Under the operation lock the first press has fully
    completed before the second begins, so this is the refusal a real replay always gets.
    """
    terminal = _Terminal()
    service = _service(tmp_path, terminal)
    record = await _untrusted(service, "claude", "decline-twice")
    await service.decline_trust(DeclineTrustCommand(record.session_id, "one-shot"))

    with pytest.raises(InvalidTransition):
        await service.decline_trust(DeclineTrustCommand(record.session_id, "one-shot"))

    assert terminal.declined == [record.session_id], "the pane was reached exactly once"


async def test_a_decline_carrying_a_spent_token_is_refused_before_the_pane_is_touched(
    tmp_path,
) -> None:
    """The claim is defence in depth, and this is the case where it is the only defence.

    The matrix refuses a replay because the session has already ended — but that guard is
    about the *session*, and DEC-011's is about the *token*. A token already spent elsewhere
    must not be able to end a session that is still perfectly declinable, and nothing about
    the record's state would stop it.
    """
    from remote_agents.application.errors import DuplicateCommandError

    terminal = _Terminal()
    service = _service(tmp_path, terminal)
    record = await _untrusted(service, "claude", "decline-spent")
    await service._store.claim_idempotency_key("already-spent")

    with pytest.raises(DuplicateCommandError):
        await service.decline_trust(DeclineTrustCommand(record.session_id, "already-spent"))

    assert terminal.declined == []
    assert (await service._store.get(record.session_id)).state is SessionState.UNTRUSTED


def test_the_decision_that_permits_an_unconfirmed_end_is_recorded() -> None:
    """A `Supersedes` that exists only in a commit message is not a recorded decision."""
    from pathlib import Path

    register = Path("/mnt/vault/Portfolio/infra/remote-agents/decisions.md")
    if not register.exists():  # pragma: no cover - the vault is not always mounted
        pytest.skip("the decision register is not mounted on this host")

    text = register.read_text(encoding="utf-8")

    assert text.count("\n## DEC-078 ") == 1
    assert "Supersedes:** DEC-018 and DEC-007" in text


async def test_a_decline_is_refused_when_the_record_is_stale_and_the_pane_has_moved_on(
    tmp_path,
) -> None:
    """DEC-078's bound is a *pair* of guards, and this is the one the matrix cannot give.

    The stored state lags the pane: a dialog answered at the keyboard, or from the other
    surface, is noticed only by a later observation. So `record.state is UNTRUSTED` is not
    evidence that the agent is still waiting, and an unconfirmed kill must not act on it
    alone. Without this the button would destroy a session doing real work — which is the one
    thing DEC-078 says it can never reach.
    """
    from remote_agents.application.errors import StopNotPermittedError

    terminal = _Terminal()
    service = _service(tmp_path, terminal)
    record = await _untrusted(service, "claude", "stale-record")
    assert record.state is SessionState.UNTRUSTED

    terminal.still_asking = False  # answered in the pane; the store has not caught up

    with pytest.raises(StopNotPermittedError):
        await service.decline_trust(DeclineTrustCommand(record.session_id, "stale-press"))

    assert terminal.declined == []
    assert (await service._store.get(record.session_id)).state is SessionState.UNTRUSTED
