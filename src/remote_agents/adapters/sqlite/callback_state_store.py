"""Durable callback state, so a token outlives the process that minted it."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import UTC, datetime

from remote_agents.ports.callback_state import UNBOUND, CallbackState

_LOG = logging.getLogger(__name__)
_COLUMNS = "token, action, entity_id, owner_id, chat_id, message_id, mutation"


class SQLiteCallbackStateStore:
    """Hold all callback meaning in the state database; the Telegram token is a lookup key.

    Two properties the process-local predecessor could not offer. A restart no longer voids
    every button in the chat — the tokens are still here, and the owner is not asked to send
    `/start` after every deploy. And the one-shot claim that keeps a mutation from running
    twice is now durable, which matters more here than the durability of the tokens
    themselves: DEC-005 permits a second process to write this store, and an in-memory claim
    set was never visible to it.

    Nothing reads `created_at`. It is recorded for eviction order and for forensics, and a
    token that has sat unused for a year resolves exactly like one minted a second ago.
    """

    def __init__(self, connection: sqlite3.Connection, *, limit: int = 20_000) -> None:
        if limit < 1:
            raise ValueError("callback state capacity must be positive")
        self._connection = connection
        self._limit = limit

    def create(
        self,
        action: str,
        entity_id: str,
        owner_id: int,
        chat_id: int,
        message_id: int = UNBOUND,
        *,
        mutation: bool = False,
    ) -> str:
        if not action or not entity_id or message_id < 0:
            raise ValueError("callback state must contain a safe action, entity, and message")
        self._evict_over_capacity()
        token = f"c1_{secrets.token_urlsafe(18)}"
        with self._connection:
            self._connection.execute(
                f"INSERT INTO callback_states({_COLUMNS}, claimed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    token,
                    action,
                    entity_id,
                    owner_id,
                    chat_id,
                    message_id,
                    int(mutation),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return token

    def bind_pending(self, chat_id: int, message_id: int) -> int:
        """Attach this chat's freshly minted tokens to the message that now carries them.

        Called once the send or edit has returned a message id. Anything still unbound in
        this chat belongs to the screen just delivered: a render mints its keyboard and
        delivers it before any other render can run, so there is no second unbound set to
        confuse it with.
        """
        if message_id <= UNBOUND:
            raise ValueError("a bound callback message must be a real Telegram message")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE callback_states SET message_id = ? WHERE chat_id = ? AND message_id = ?",
                (message_id, chat_id, UNBOUND),
            )
        return cursor.rowcount

    def resolve(
        self, token: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> CallbackState | None:
        """Return what a token means, only for the exact owner, chat, and message that own it."""
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM callback_states "
            "WHERE token = ? AND owner_id = ? AND chat_id = ? AND message_id = ?",
            (token, owner_id, chat_id, message_id),
        ).fetchone()
        if row is None:
            return None
        return CallbackState(row[0], row[1], row[2], row[3], row[4], row[5], bool(row[6]))

    def claim_mutation(self, token: str, *, owner_id: int, chat_id: int, message_id: int) -> bool:
        """Claim a mutating token exactly once, for one writer, in one statement.

        The claim is the `UPDATE` itself rather than a read followed by a write: SQLite
        serializes writers, so the second caller — in this process or the other one DEC-005
        permits — matches no row and is refused. A `SELECT` then `UPDATE` would leave both
        callers reading `claimed = 0` and both proceeding, which is the double-launch and
        double-stop this method exists to prevent — DEC-007's "no repeated keypress
        destroys anything" is the decision that binds this surface, and DEC-008 states the
        same principle for the local one.

        **`message_id` is accepted and deliberately not matched on.** Message binding is the
        *resolution* rule (DEC-011), and `resolve` has already enforced it for this exact
        token, owner, chat and message before any caller reaches here — all six call sites
        resolve first. Re-checking it at the claim was a second enforcement of a fact already
        established, and it was the half that broke: `LiveView.move_to_bottom` `rebind`s the
        anchor's tokens whenever a notification pushes the screen down, and every action that
        shows a pending screen waits a Telegram round trip between resolving and claiming. A
        rebind inside that gap left the claim matching no row, so a launch, stop or resume
        that never ran answered "That action has already run" — the rebind that exists to keep
        the keyboard working across the move breaking the one thing that did not survive it.

        Owner and chat stay. They are the half that is about *authorization* rather than about
        which message is currently on screen, and neither changes under a rebind, so keeping
        them costs nothing and preserves the defence if a caller ever reaches here without
        resolving first.

        The parameter is kept in the signature rather than removed: every caller has it, it
        documents what the claim is *about*, and dropping it would silently change six call
        sites whose correctness depends on having resolved that message first.
        """
        del message_id
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE callback_states SET claimed = 1 "
                "WHERE token = ? AND owner_id = ? AND chat_id = ? "
                "AND mutation = 1 AND claimed = 0",
                (token, owner_id, chat_id),
            )
        return cursor.rowcount == 1

    def prune_for_message(self, chat_id: int, message_id: int) -> int:
        """Discard the tokens of a message that no longer exists, and report how many.

        This is the retention rule that replaces the TTL: a token dies with its message, not
        on a clock. A superseded screen the service deleted can carry no live button, so
        keeping its tokens would only grow the table.
        """
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM callback_states WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
        return cursor.rowcount

    def rebind(self, chat_id: int, from_message_id: int, to_message_id: int) -> int:
        """Move a message's tokens onto the message replacing it, and report how many.

        One statement, so a concurrent writer cannot see the tokens belonging to neither
        message. The durable half of the pair matters here: the live view moves to the bottom
        of the chat whenever a notification arrives, and a keyboard that stopped resolving
        across that move would be the dead-button defect sub-plan 1 exists to have removed.
        """
        if to_message_id <= UNBOUND:
            raise ValueError("a rebound callback message must be a real Telegram message")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE callback_states SET message_id = ? WHERE chat_id = ? AND message_id = ?",
                (to_message_id, chat_id, from_message_id),
            )
        return cursor.rowcount

    def active_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM callback_states").fetchone()[0])

    def _evict_over_capacity(self) -> None:
        """Keep the table bounded by size, since it is no longer bounded by time.

        The predecessor refused to mint a token once full, which turned a full table into a
        dead keyboard. Evicting the oldest instead degrades the one thing that can be
        degraded — a button nobody has pressed in twenty thousand renders — and logs once per
        pass rather than once per token, so a chatty log cannot be produced by this path.
        """
        surplus = self.active_count() - self._limit + 1
        if surplus <= 0:
            return
        with self._connection:
            self._connection.execute(
                "DELETE FROM callback_states WHERE token IN ("
                "SELECT token FROM callback_states ORDER BY created_at, rowid LIMIT ?)",
                (surplus,),
            )
        _LOG.info("evicted %d callback states over the %d capacity", surplus, self._limit)
