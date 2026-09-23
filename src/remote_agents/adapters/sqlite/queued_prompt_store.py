"""The relay's queue: one waiting message per session, in the UI store (DEC-090, DEC-099).

In `ui.sqlite3` and not the watched domain store, because a row here is the bot's own
bookkeeping: written into the watched file, every queued message would wake the store watcher
and redraw the bot on its own write -- the loop DEC-090 exists to prevent.

**A delivery claims its row rather than deleting it.** The row stays, marked in flight
(`claimed_at`), until the delivery settles it. So the owner's cancel still reaches a message
whose delivery is under way, and a refused delivery's `restore` can only un-mark the very claim
it made: a message cancelled or replaced meanwhile never comes back. A claim abandoned by a crash
is claimable again once it is older than `_ABANDONED`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from remote_agents.ports.queued_prompts import QueuedPrompt

_ABANDONED = timedelta(minutes=2)
"""How old an in-flight claim must be before it is treated as abandoned. Far longer than one
delivery, which is bounded at `TerminalWaits.prompt_bound` (20 s)."""


class SQLiteQueuedPromptStore:
    """At most one waiting message per session; the newest one is the one that waits."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def queue(self, session_id: str, text: str) -> bool:
        with self._immediate():
            replaced = self._pending_row(session_id) is not None
            # A newer message is not in flight, whatever the one it replaces was.
            self._connection.execute(
                """
                INSERT INTO queued_prompts(session_id, text, queued_at, claimed_at)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    text = excluded.text, queued_at = excluded.queued_at, claimed_at = NULL
                """,
                (session_id, text, _now().isoformat()),
            )
        return replaced

    def pending(self, session_id: str) -> QueuedPrompt | None:
        row = self._pending_row(session_id)
        return None if row is None else _prompt(row)

    def claim(self, session_id: str, *, now: datetime | None = None) -> QueuedPrompt | None:
        moment = (now or _now()).astimezone(UTC)
        # One statement: the row is marked and returned together, so two claimers cannot both
        # take it.
        with self._connection:
            row = self._connection.execute(
                """
                UPDATE queued_prompts SET claimed_at = ?
                WHERE session_id = ? AND (claimed_at IS NULL OR claimed_at < ?)
                RETURNING session_id, text, queued_at, claimed_at
                """,
                (moment.isoformat(), session_id, (moment - _ABANDONED).isoformat()),
            ).fetchone()
        return None if row is None else _prompt(row)

    def restore(self, prompt: QueuedPrompt) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE queued_prompts SET claimed_at = NULL
                WHERE session_id = ? AND queued_at = ? AND claimed_at = ?
                """,
                (prompt.session_id, prompt.queued_at.isoformat(), _claim(prompt)),
            )
        return cursor.rowcount == 1

    def settle(self, prompt: QueuedPrompt) -> None:
        """Remove a claimed message whose delivery is over -- sent, or never to be retried."""
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM queued_prompts
                WHERE session_id = ? AND queued_at = ? AND claimed_at = ?
                """,
                (prompt.session_id, prompt.queued_at.isoformat(), _claim(prompt)),
            )

    def cancel(self, session_id: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM queued_prompts WHERE session_id = ?", (session_id,)
            )
        return cursor.rowcount == 1

    def clear(self, session_id: str) -> None:
        self.cancel(session_id)

    def waiting(self) -> tuple[QueuedPrompt, ...]:
        """Every waiting message, in flight or not -- for the sweep that clears ended sessions."""
        rows = self._connection.execute(
            "SELECT session_id, text, queued_at, claimed_at FROM queued_prompts ORDER BY queued_at"
        ).fetchall()
        return tuple(_prompt(row) for row in rows)

    def _pending_row(self, session_id: str) -> tuple[str, str, str, str | None] | None:
        return self._connection.execute(
            """
            SELECT session_id, text, queued_at, claimed_at FROM queued_prompts
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

    @contextmanager
    def _immediate(self):
        """A write transaction taken before the read, so the read and the write are one.

        Depends on no transaction being open on this shared connection when it starts, which
        every store on it keeps by writing only inside `with connection:` -- a raw write left
        uncommitted by another adapter would make this raise "cannot start a transaction within
        a transaction". Loud rather than silent, but a convention every future adapter on the UI
        connection must keep.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()


def _now() -> datetime:
    return datetime.now(UTC)


def _prompt(row: tuple[str, str, str, str | None]) -> QueuedPrompt:
    session_id, text, queued_at, claimed_at = row
    return QueuedPrompt(
        session_id,
        text,
        datetime.fromisoformat(queued_at),
        None if claimed_at is None else datetime.fromisoformat(claimed_at),
    )


def _claim(prompt: QueuedPrompt) -> str:
    """The claim a restore or settle must match; a message never claimed matches nothing."""
    return "" if prompt.claimed_at is None else prompt.claimed_at.isoformat()
