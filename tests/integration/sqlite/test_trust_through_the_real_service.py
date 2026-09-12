"""Both answers to the folder-trust question, driven through the real `SessionService`.

**Why a real service and a real store, and not a fake launcher.** This file exists because of
a specific escape. The trust *answer* had three profile gates — the policy, the tmux runtime,
and the service — and the first two were widened to accept `claude-remote` while the third was
not. Every test passed: the unit tests exercised the policy, the e2e journey drove a *fake*
launcher with no profile gate at all, and 1709 tests were green while the button rendered and
then refused itself on the only session that ever needed it. A fake that omits the layer
holding the bug cannot see the bug.

The *decline* half was added here for the same reason, and DEC-078 makes the stakes higher: it
is the one action on this control plane that ends a session with no confirmation step, so the
guards that bound it have to be exercised against the real matrix and the real store rather
than against a double that would agree with anything.

**The answer half's two tests were briefly deleted from this file and are restored.** They
were lost to a whole-file overwrite while the decline tests were being added — which is exactly
the escape described above, reopened in the same change that hardened its sibling. They are
first in the file now, ahead of the newer material, so the next person to extend it appends.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_trust_dialogs
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.commands import (
    AnswerTrustCommand,
    DeclineTrustCommand,
    LaunchCommand,
)
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import InvalidTransition
from remote_agents.domain.trust import TrustState
from remote_agents.ports.terminal import NOT_AWAITING_TRUST, TerminalObservation, TrustAnswer


class _AnsweringTerminal:
    def __init__(self) -> None:
        self.answered: list[SessionId] = []

    async def trust_state(self, session_id: SessionId) -> TrustState:
        del session_id
        return TrustState.AWAITING

    async def answer_trust(self, session_id: SessionId) -> TrustAnswer:
        """What the real runtime returns on the success path, and it has to stay that.

        A double that returns a bare `TrustState` here keeps this file's assertions passing
        while no longer standing in for the port -- and this file's whole point is that the
        service is exercised through a real store rather than through a fake that omits the
        layer holding the bug. Migrated with `TrustAnswer` (BL-053) for that reason.
        """
        self.answered.append(session_id)
        return TrustAnswer(pressed=True, observed=TrustState.UNKNOWN)


def _answerable_record(profile: str) -> SessionRecord:
    """A session the lifecycle records as blocked on the question — which is what makes it one.

    `SessionState.FAILED` until 2026-09-10, because that is where a trust-blocked launch landed
    before sub-plan 1 gave it a state of its own, and because the service did not ask. It asks
    now (DEC-080): a press is refused unless the record says `untrusted`, so a FAILED record is
    no longer an answerable one — a FAILED session whose pane really is asking becomes
    `untrusted` on the next reconciliation pass and is answerable then.
    """
    return SessionRecord(
        SessionId.new(),
        ProjectId("a" * 24),
        ProfileId(profile),
        SessionDisplayIdentity("Demo", profile, "regular", 1),
        SessionState.UNTRUSTED,
        datetime.now(UTC),
    )


def _store(tmp_path: Path) -> SQLiteSessionStore:
    return SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))


# --- the answer half -------------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(profile_trust_dialogs()))
async def test_every_answerable_profile_is_answerable_through_the_real_service(
    tmp_path: Path, profile: str
) -> None:
    """Parametrized over the authority itself, so a profile added there is covered here.

    This is the assertion whose absence let `claude-remote` through: it was in the policy's
    set and refused by the service, and nothing compared the two.

    The authority is no longer a list to add to — it is "which verticals declare a dialog"
    (`profile_trust_dialogs`), so this now covers codex and cursor-agent because they declare
    one, not because anybody remembered to widen a frozenset. That is the same failure in its
    other direction: the list was right about claude-remote and silently wrong about the two
    agents that learned to ask afterwards.
    """
    store = _store(tmp_path)
    record = _answerable_record(profile)
    await store.save(record)
    terminal = _AnsweringTerminal()
    service = SessionService(store, terminal)

    result = await service.answer_trust(AnswerTrustCommand(record.session_id, f"key-{profile}"))

    assert terminal.answered == [record.session_id], f"{profile} never reached the terminal"
    assert result.pressed, "the terminal answered, so the service must report that it did"
    assert result.observed is TrustState.UNKNOWN


async def test_a_list_open_does_not_relabel_an_aged_failed_session(tmp_path: Path) -> None:
    """**The faster door, shut.** `refresh_readiness` runs on every session-list open.

    The bound that keeps a screenful of text from rewriting a working session's record lived
    only in `ReconciliationService`, which looks once a minute. This path looks every time
    either surface renders a list, and its FAILED arm corrected to `untrusted` with no bound at
    all — so the hazard DEC-080 was written for survived, through a door that opens faster than
    the one that was closed. Found by an adversarial review after the first fix.

    A FAILED record holding a live, working agent is not hypothetical: every opencode launch was
    one, for months, because a readiness marker was three ASCII dots where the agent draws an
    ellipsis. `untrusted` carries DEC-078's unconfirmed kill, so this is a one-press loss of
    real work.
    """
    store = _store(tmp_path)
    aged = replace(
        _answerable_record("codex"),
        state=SessionState.FAILED,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    await store.save(aged)

    class _Blocked:
        async def confirm_ready(self, session_id, profile_id):
            del profile_id
            return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    service = SessionService(store, _Blocked())

    await service.refresh_readiness()

    assert (await store.get(aged.session_id)).state is SessionState.FAILED, (
        "a three-hour-old FAILED session was relabelled untrusted by a list render, which "
        "hands a working agent a one-press unconfirmed kill"
    )


async def test_a_fresh_failed_session_is_still_corrected_on_a_list_open(tmp_path: Path) -> None:
    """The bound narrows *when*, not *whether* — the rescue path still works while it is fresh."""
    store = _store(tmp_path)
    fresh = replace(_answerable_record("codex"), state=SessionState.FAILED)
    await store.save(fresh)

    class _Blocked:
        async def confirm_ready(self, session_id, profile_id):
            del profile_id
            return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    service = SessionService(store, _Blocked())

    await service.refresh_readiness()

    assert (await store.get(fresh.session_id)).state is SessionState.UNTRUSTED


async def test_the_two_late_dialog_windows_are_the_same_number(tmp_path: Path) -> None:
    """Two copies of one bound, in two modules, because reconcile imports services.

    The duplication is a deliberate cycle avoidance and this is what keeps it honest: a window
    widened in one place and not the other would leave the faster door open again, which is
    exactly how this defect survived its first fix.
    """
    del tmp_path
    from remote_agents.application.reconcile import _LATE_DIALOG_WINDOW as reconciler
    from remote_agents.application.services import _LATE_DIALOG_WINDOW as service

    assert reconciler == service, (reconciler, service)


async def test_a_session_that_is_no_longer_untrusted_is_refused_the_answer(tmp_path: Path) -> None:
    """The stale button, closed where lifecycle policy lives rather than on one surface.

    The bot no longer *renders* the row for a session that is not `untrusted` (DEC-080), but a
    token minted while it was one outlives the screen that drew it: the owner answers the dialog
    at the keyboard, the record moves on, and the old button is still in the chat. Pressing it
    would reach a session that is running real work, and the terminal's pane re-read is then the
    only thing between that and a keypress — which is exactly the evidence DEC-080 says cannot
    tell a drawn dialog from a file that quotes one.

    So the state is asked here too, once, for every surface and every future caller.
    """
    store = _store(tmp_path)
    record = replace(_answerable_record("claude"), state=SessionState.RUNNING)
    await store.save(record)
    terminal = _AnsweringTerminal()
    service = SessionService(store, terminal)

    with pytest.raises(ValueError, match="not waiting"):
        await service.answer_trust(
            AnswerTrustCommand(record.session_id, idempotency_key="stale-token")
        )

    assert terminal.answered == [], "a keypress reached a session that had moved on"


async def test_the_service_delegates_every_profile_to_the_terminal(tmp_path: Path) -> None:
    """**The bound moved down a layer; it was not deleted, and this is where that is visible.**

    This test read "a profile that never asks is refused by the service" and drove it with
    `codex`, expecting `ValueError("only for Claude")`. Both halves stopped being true at once:
    codex asks, and the service no longer holds a profile list of its own. Keeping the old
    assertion would have meant keeping the list.

    What replaces it is the property that makes the removal safe: the service delegates every
    profile, and the *terminal* is the single authority — it holds a dialog for exactly the
    profiles that can be read, so an agent that asks nothing gets `UNKNOWN` and no keypress.
    That refusal is driven against a real `TmuxTerminal` in
    `tests/contract/adapters/tmux/test_resume_readiness.py`, which is the layer that can see a
    pane; asserting it here, over a fake terminal, would assert the fake.
    """
    store = _store(tmp_path)
    record = _answerable_record("codex")
    await store.save(record)
    terminal = _AnsweringTerminal()
    service = SessionService(store, terminal)

    await service.answer_trust(
        AnswerTrustCommand(record.session_id, idempotency_key="answer-codex")
    )

    assert terminal.answered == [record.session_id], (
        "the service refused a profile of its own accord, which is the second authority the "
        "derivation exists to remove"
    )


# --- the decline half ------------------------------------------------------------


class _Terminal:
    """Records which route a decline took, because the two are the whole of the behaviour."""

    def __init__(self, *, awaiting_trust: bool = True) -> None:
        self.awaiting_trust = awaiting_trust
        self.declined: list[SessionId] = []

    async def launch(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        *,
        remote_control: bool = False,
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
    return (
        await service.launch(LaunchCommand(ProjectId("opaque-editor"), ProfileId(profile), key))
    ).record


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
    """The *no* ends the session whichever route the terminal takes to deliver it.

    codex now declares its own dialog, so the terminal tells it no in its own words rather
    than killing the pane — but this test is not about that, and deliberately does not assert
    it: the lifecycle does not care which route was taken, and that indifference is the whole
    reason the decline is a lifecycle event rather than a keystroke. The route itself is
    pinned where it can be seen, against the real captures, in
    `tests/contract/adapters/tmux/test_resume_readiness.py`.
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
    record = (
        await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "running-one")
        )
    ).record
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
    # Clause-level, which is this register's idiom and which the first draft of the entry got
    # wrong: it named two entries and no clause of either, and DEC-018 turned out not to be
    # superseded at all. Asserted on the clause so a future widening of the claim has to
    # change a line that says what is actually being given up.
    assert "DEC-007's confirmation mitigation, one clause, for `UNTRUSTED` only" in text
    assert "DEC-018 is cited here and is deliberately *not* superseded" in text


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


async def test_a_refused_decline_does_not_move_the_owner_s_console(tmp_path) -> None:
    """The ordering finding, pinned. A refusal must not cost the owner their view.

    `force_stop` stands the console down *before* the terminal call, because DEC-017 makes it
    end the record either way — there is no path where it moved the surface for nothing. This
    method has exactly such a path: the stale-record refusal, which is the case DEC-078 is
    built around. Standing down first would kick the console off a pane whose agent is
    working, for a decline that was then refused.
    """
    from remote_agents.application.errors import StopNotPermittedError

    terminal = _Terminal()
    stood_down: list[SessionId] = []

    async def _hide(session_id: SessionId) -> None:
        stood_down.append(session_id)

    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "s.sqlite3")),
        terminal,
        hide_in_console=_hide,
    )
    record = (
        await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "refused-decline")
        )
    ).record
    terminal.still_asking = False

    with pytest.raises(StopNotPermittedError):
        await service.decline_trust(DeclineTrustCommand(record.session_id, "refused-press"))

    assert stood_down == [], "a refused decline moved the console anyway"


async def test_a_decline_that_lands_does_stand_the_console_down(tmp_path) -> None:
    """The other half: the session really ending must still release the console's slot."""
    terminal = _Terminal()
    stood_down: list[SessionId] = []

    async def _hide(session_id: SessionId) -> None:
        stood_down.append(session_id)

    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "s.sqlite3")),
        terminal,
        hide_in_console=_hide,
    )
    record = (
        await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "landed-decline")
        )
    ).record

    await service.decline_trust(DeclineTrustCommand(record.session_id, "landed-press"))

    assert stood_down == [record.session_id]
