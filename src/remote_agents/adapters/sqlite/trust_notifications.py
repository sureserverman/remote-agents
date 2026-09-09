"""SQLite home for the standing folder-trust question (migration 12)."""

from __future__ import annotations

import sqlite3

from remote_agents.domain.models import SessionId
from remote_agents.ports.trust_notifications import StandingTrustQuestion


class SQLiteTrustNotificationStore:
    """One row per session, so a restart does not ask the same question twice.

    Keyed on the session alone, unlike `standing_notifications`, and the difference is what the
    two messages are about: an activity notification is about a chat's view of a session, and
    the trust question is about the session itself, which has exactly one answer whoever is
    looking. `remember` therefore replaces rather than inserts — a later render supersedes an
    earlier one for the same session rather than standing beside it.

    Writes go through `with self._connection:` rather than a bare `execute` plus `commit`,
    which is the house idiom every sibling store here uses and is not cosmetic: it rolls back
    on any exception rather than leaving a half-finished transaction open on a connection that
    is shared and long-lived, where the next unrelated write would inherit it.

    **No delete path, and the table therefore grows without bound** -- one row per session ever
    asked. Said out loud rather than left to be discovered, which is the note migration 9 makes
    about `agent_activity` for the same reason. At this service's scale (a single owner's
    sessions on one host) that is a row per launch into an untrusted folder, and retention is
    future work rather than a defect.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def remember(self, session_id: SessionId, *, chat_id: int, message_id: int) -> None:
        """Record the message now standing for this session's question, asked or re-asked.

        `settled = 0` in the conflict clause is not redundant with the insert's `0`: it is what
        makes a *re-asked* session askable again. A session can enter UNTRUSTED, be answered,
        and enter it a second time -- a later launch into the same never-trusted folder -- and
        without the reset a stale `settled = 1` would suppress a genuinely new question
        forever.
        """
        with self._connection:
            self._connection.execute(
                "INSERT INTO trust_notifications(session_id, chat_id, message_id, settled)"
                " VALUES (?, ?, ?, 0)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " chat_id = excluded.chat_id, message_id = excluded.message_id, settled = 0",
                (str(session_id), chat_id, message_id),
            )

    async def standing_for(self, session_id: SessionId) -> StandingTrustQuestion | None:
        row = self._connection.execute(
            "SELECT session_id, chat_id, message_id, settled FROM trust_notifications"
            " WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        return None if row is None else _question(row)

    async def unsettled(self) -> tuple[StandingTrustQuestion, ...]:
        rows = self._connection.execute(
            "SELECT session_id, chat_id, message_id, settled FROM trust_notifications"
            " WHERE settled = 0 ORDER BY rowid"
        ).fetchall()
        return tuple(_question(row) for row in rows)

    async def settle(self, session_id: SessionId) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE trust_notifications SET settled = 1 WHERE session_id = ?",
                (str(session_id),),
            )


def _question(row: tuple[str, int, int, int]) -> StandingTrustQuestion:
    return StandingTrustQuestion(SessionId.parse(row[0]), row[1], row[2], bool(row[3]))
