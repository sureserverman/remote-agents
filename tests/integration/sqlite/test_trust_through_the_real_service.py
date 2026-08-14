"""The trust answer, driven through the real `SessionService` rather than a fake launcher.

This file exists because of a specific escape. The feature had three profile gates -- the
policy, the tmux runtime, and the service -- and the first two were widened to accept
`claude-remote` while the third was not. Every test passed: the unit tests exercised the
policy, the e2e journey drove a *fake* launcher that had no profile gate at all, and 1709
tests were green while the button rendered and then refused itself on the only session that
ever needed it.

A fake that omits the layer holding the bug cannot see the bug. So this wires the real
service against a real store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.commands import AnswerTrustCommand
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.trust import TRUST_ANSWERABLE, TrustState


class _AnsweringTerminal:
    def __init__(self) -> None:
        self.answered: list[SessionId] = []

    async def trust_state(self, session_id: SessionId) -> TrustState:
        del session_id
        return TrustState.AWAITING

    async def answer_trust(self, session_id: SessionId) -> TrustState:
        self.answered.append(session_id)
        return TrustState.UNKNOWN


def _record(profile: str) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("a" * 24),
        ProfileId(profile),
        SessionDisplayIdentity("Demo", profile, "regular", 1),
        SessionState.FAILED,
        datetime.now(UTC),
    )


def _store(tmp_path: Path) -> SQLiteSessionStore:
    return SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))


@pytest.mark.parametrize("profile", sorted(str(p) for p in TRUST_ANSWERABLE))
async def test_every_answerable_profile_is_answerable_through_the_real_service(
    tmp_path: Path, profile: str
) -> None:
    """Parametrized over the authority itself, so a profile added there is covered here.

    This is the assertion whose absence let `claude-remote` through: it was in the policy's
    set and refused by the service, and nothing compared the two.
    """
    store = _store(tmp_path)
    record = _record(profile)
    await store.save(record)
    terminal = _AnsweringTerminal()
    service = SessionService(store, terminal)

    result = await service.answer_trust(AnswerTrustCommand(record.session_id, f"key-{profile}"))

    assert terminal.answered == [record.session_id], f"{profile} never reached the terminal"
    assert result is TrustState.UNKNOWN


async def test_a_profile_that_never_asks_is_refused_by_the_service(tmp_path: Path) -> None:
    """The gate still bites, so widening it did not simply delete it."""
    store = _store(tmp_path)
    record = _record("codex")
    await store.save(record)
    terminal = _AnsweringTerminal()
    service = SessionService(store, terminal)

    with pytest.raises(ValueError, match="only for Claude"):
        await service.answer_trust(AnswerTrustCommand(record.session_id, "key-codex"))

    assert terminal.answered == [], "a refused profile must not reach the terminal"
