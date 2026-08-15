"""Parameterized SQLite persistence for session projections and append-only events."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import TERMINAL_STATES, LifecycleEvent, transition
from remote_agents.ports.session_store import ProjectUsage

_LOG = logging.getLogger(__name__)


class SQLiteSessionStore:
    """Store durable metadata only; terminal pane content never enters this adapter."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        """Allocate the next persisted display sequence for one project/profile pair."""
        result = self._connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_id = ? AND profile_id = ?",
            (str(project_id), str(profile_id)),
        ).fetchone()
        return int(result[0]) + 1

    async def save(self, record: SessionRecord) -> None:
        """Insert a session projection using bound values rather than SQL interpolation."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO sessions(
                    session_id, project_id, profile_id, display_identity, state, created_at,
                    resume_profile_id, resume_source_id, terminal_reason, orphan_provenance
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.session_id),
                    str(record.project_id),
                    str(record.profile_id),
                    record.display.rendered,
                    record.state.value,
                    record.created_at.astimezone(UTC).isoformat(),
                    str(record.resume_profile_id) if record.resume_profile_id else None,
                    record.resume_source_id,
                    record.terminal_reason,
                    record.orphan_provenance.value if record.orphan_provenance else None,
                ),
            )

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        """Load one immutable projection by its opaque session identifier."""
        row = self._connection.execute(
            """
            SELECT session_id, project_id, profile_id, display_identity, state, created_at,
                   resume_profile_id, resume_source_id, terminal_reason, orphan_provenance
            FROM sessions WHERE session_id = ?
            """,
            (str(session_id),),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def get_by_resume_source(
        self, profile_id: ProfileId, source_id: str
    ) -> SessionRecord | None:
        row = self._connection.execute(
            """
            SELECT session_id, project_id, profile_id, display_identity, state, created_at,
                   resume_profile_id, resume_source_id, terminal_reason, orphan_provenance
            FROM sessions WHERE resume_profile_id = ? AND resume_source_id = ?
            """,
            (str(profile_id), source_id),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def list(self, states: Collection[SessionState] | None = None) -> Sequence[SessionRecord]:
        """Return durable projections, optionally filtered by approved lifecycle states."""
        query = (
            "SELECT session_id, project_id, profile_id, display_identity, state, created_at, "
            "resume_profile_id, resume_source_id, terminal_reason, orphan_provenance "
            "FROM sessions"
        )
        values: tuple[str, ...] = ()
        if states:
            values = tuple(state.value for state in states)
            query += f" WHERE state IN ({', '.join('?' for _ in values)})"
        rows = self._connection.execute(query, values).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        """Append a lifecycle event and atomically refresh its session projection."""
        current = await self.get(session_id)
        if current is None:
            raise KeyError(f"unknown session: {session_id}")
        to_state = transition(current.state, event).to_state
        # Only a terminal state gets a reason, and only the first one: the matrix offers no
        # way out of ENDED or ORPHANED, so the event that landed there is the whole answer
        # to why the session stopped.
        terminal_reason = event.value if to_state in TERMINAL_STATES else current.terminal_reason
        # The second of ORPHANED's two producers. `reconcile._save_trusted_orphan` stamps
        # ADOPTED when it *creates* a record; the ambiguous producer never creates one, it
        # transitions an existing record, and this is the method that lands it. Stamping here
        # is what lets a NULL provenance mean one thing (a row older than migration 6) rather
        # than three. Existing provenance is never overwritten, because it is durable history:
        # a record moving to ENDED under DEC-020's force stop has to carry out of ORPHANED
        # which kind of ORPHANED it was, or the audit trail loses what was killed.
        orphan_provenance = current.orphan_provenance
        if to_state is SessionState.ORPHANED and orphan_provenance is None:
            orphan_provenance = OrphanProvenance.AMBIGUOUS
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            current.display,
            to_state,
            current.created_at,
            current.resume_profile_id,
            current.resume_source_id,
            terminal_reason,
            orphan_provenance,
        )
        with self._connection:
            self._append_event(str(session_id), event)
            self._connection.execute(
                """
                UPDATE sessions SET state = ?, terminal_reason = ?, orphan_provenance = ?
                WHERE session_id = ?
                """,
                (
                    updated.state.value,
                    updated.terminal_reason,
                    updated.orphan_provenance.value if updated.orphan_provenance else None,
                    str(session_id),
                ),
            )
        return updated

    def append_event(
        self,
        session_id: str,
        event: LifecycleEvent,
        *,
        idempotency_key: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Append a sanitized lifecycle event without pane, prompt, token, or environment data."""
        with self._connection:
            self._append_event(
                session_id,
                event,
                idempotency_key=idempotency_key,
                error_code=error_code,
            )

    def _append_event(
        self,
        session_id: str,
        event: LifecycleEvent,
        *,
        idempotency_key: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Write a validated event within the caller's transaction boundary."""
        if error_code and any(
            fragment in error_code.casefold() for fragment in ("token", "prompt", "pane", "env")
        ):
            raise ValueError("event error code must be sanitized")
        self._connection.execute(
            """
            INSERT INTO session_events(
                session_id, event_type, created_at, idempotency_key, error_code
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                event.value,
                datetime.now(UTC).isoformat(),
                idempotency_key,
                error_code,
            ),
        )

    async def set_label(self, session_id: SessionId, label: str | None) -> SessionRecord:
        """Rename a session, or clear its name, by rewriting the identity it already stores.

        The label is the optional fifth part of `display_identity`, not a column of its own, so
        this is an UPDATE of an existing column rather than a migration. `SessionDisplayIdentity`
        re-validates on construction, which is what makes the write safe: an invalid label
        raises here, before the UPDATE, so a rejected rename leaves the row exactly as it was.

        Raises `KeyError` for a session that is not stored. Answering "renamed" for a session
        that does not exist would let a stale button report success against nothing — the same
        fail-dangerous default the stop path removed.
        """
        current = await self.get(session_id)
        if current is None:
            raise KeyError(f"unknown session: {session_id}")
        display = SessionDisplayIdentity(
            current.display.project_slug,
            current.display.agent_label,
            current.display.mode,
            current.display.sequence,
            label,
        )
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            display,
            current.state,
            current.created_at,
            current.resume_profile_id,
            current.resume_source_id,
            current.terminal_reason,
            current.orphan_provenance,
        )
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET display_identity = ? WHERE session_id = ?",
                (display.rendered, str(session_id)),
            )
        return updated

    async def project_usage(self) -> tuple[ProjectUsage, ...]:
        """Summarize every project's whole launch history in one aggregate, not one per project.

        A ranking is drawn for a page of projects, so the naive shape — count the rows for the
        project you are about to render — is a query per rendered row, paid again on every
        refresh. This is one GROUP BY over the table instead: the statement count does not move
        when the catalogue grows.

        Projects with no sessions are simply absent. There is no row to group, and inventing a
        zero would be a claim this table cannot make — the sessions table knows nothing about
        which projects exist, so an absent project here means "never launched from here", not
        "not a project".

        `MAX(created_at)` is a lexicographic max over the ISO strings `save` writes. That is
        the chronological max because every one of them is normalized to UTC before it is
        stored, so they share an offset and a field order; it would not be, were a local offset
        ever written into this column.
        """
        rows = self._connection.execute(
            """
            SELECT project_id, COUNT(*), MAX(created_at)
            FROM sessions
            GROUP BY project_id
            ORDER BY project_id
            """
        ).fetchall()
        return tuple(
            ProjectUsage(ProjectId(row[0]), int(row[1]), _instant_from_row(row[2])) for row in rows
        )

    async def claim_idempotency_key(self, key: str) -> bool:
        """Atomically claim a callback key without creating a fake session event."""
        with self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO idempotency_claims(key, created_at) VALUES (?, ?)",
                    (key, datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True


def _instant_from_row(value: str) -> datetime:
    """Read a stored timestamp back as an aware instant, even if the row predates the rule.

    Everything `save` writes is UTC, so a column value without an offset is an old or
    hand-edited row rather than a local time — reading it as UTC is the honest interpretation,
    and `astimezone` would instead silently reinterpret it in the machine's zone.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _record_from_row(
    row: tuple[str, str, str, str, str, str, str | None, str | None, str | None, str | None],
) -> SessionRecord:
    """Rebuild a validated domain record from one trusted SQLite projection row."""
    display_parts = row[3].split(" · ", 4)
    if len(display_parts) not in {4, 5} or not display_parts[3].startswith("#"):
        raise ValueError("stored display identity is invalid")
    display = SessionDisplayIdentity(
        display_parts[0],
        display_parts[1],
        display_parts[2],
        int(display_parts[3][1:]),
        display_parts[4] if len(display_parts) == 5 else None,
    )
    return SessionRecord(
        SessionId.parse(row[0]),
        ProjectId(row[1]),
        ProfileId(row[2]),
        display,
        SessionState(row[4]),
        datetime.fromisoformat(row[5]),
        ProfileId(row[6]) if row[6] is not None else None,
        row[7],
        row[8],
        _provenance_from_row(row[9]),
    )


def _provenance_from_row(value: str | None) -> OrphanProvenance | None:
    """Read a stored provenance, falling to the conservative branch on anything unknown.

    An unrecognized value is a hand-edited row, a partial write, or — the realistic case — a
    member written by a newer build and read back after a downgrade. Raising here would be
    the wrong kind of strict: `list` rebuilds every row in one comprehension, so a single bad
    value would take out the whole session list on both surfaces *and* stop reconciliation
    from reaching every other session on the tick.

    Refusing is not the only alternative to guessing permissively, which is how this was first
    written. This column decides whether a destructive action is offered, and `None` is the
    branch that offers *less*, so falling to it is exactly as safe as refusing and keeps the
    session visible and actionable in every other respect. The value is logged rather than
    swallowed, because a row nobody can explain is worth knowing about.
    """
    if value is None:
        return None
    try:
        return OrphanProvenance(value)
    except ValueError:
        _LOG.warning(
            "unrecognized orphan provenance %r; treating it as the conservative branch", value
        )
        return None
