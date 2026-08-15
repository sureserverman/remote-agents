"""ORPHANED is two situations, and which one it is has to survive the round trip.

DEC-020 makes the action policy branch on *which producer* created an ORPHANED record, so
the branch is only as good as the storage under it. The rebuild tests here exist because
`record_event` and `set_label` reconstruct `SessionRecord` **positionally**: a field appended
after `terminal_reason` is exactly the shape those two silently drop, and a dropped
provenance downgrades an adopted record to the conservative branch with nothing failing.
"""

import sqlite3
from datetime import UTC, datetime

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent


def _adopted(session_id: SessionId) -> SessionRecord:
    """The shape `reconcile._save_trusted_orphan` writes: a live pane the register lost."""
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "recovered", 1),
        SessionState.ORPHANED,
        datetime.now(UTC),
        orphan_provenance=OrphanProvenance.ADOPTED,
    )


async def test_an_adopted_orphan_keeps_its_provenance_across_a_reload(tmp_path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()

    await store.save(_adopted(session_id))

    reloaded = await store.get(session_id)
    assert reloaded is not None
    assert reloaded.orphan_provenance is OrphanProvenance.ADOPTED


async def test_every_read_path_returns_the_provenance_not_only_get(tmp_path) -> None:
    """`get`, `list` and `get_by_resume_source` each name their own column list."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    adopted = _adopted(session_id)
    await store.save(
        SessionRecord(
            adopted.session_id,
            adopted.project_id,
            adopted.profile_id,
            adopted.display,
            adopted.state,
            adopted.created_at,
            ProfileId("claude"),
            "source-123",
            None,
            OrphanProvenance.ADOPTED,
        )
    )

    listed = await store.list()
    by_resume = await store.get_by_resume_source(ProfileId("claude"), "source-123")

    assert [record.orphan_provenance for record in listed] == [OrphanProvenance.ADOPTED]
    assert by_resume is not None
    assert by_resume.orphan_provenance is OrphanProvenance.ADOPTED


async def test_renaming_an_adopted_orphan_keeps_its_provenance(tmp_path) -> None:
    """`set_label` rebuilds the record positionally, so the tenth field is what it drops."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(_adopted(session_id))

    updated = await store.set_label(session_id, "night-run")

    assert updated.orphan_provenance is OrphanProvenance.ADOPTED
    assert updated == await store.get(session_id)


async def test_recording_an_event_keeps_the_provenance_it_was_saved_with(tmp_path) -> None:
    """The other positional rebuild. Driven from a non-ORPHANED state deliberately.

    ORPHANED has no outgoing transition until Task 4.2 adds one, so a record carrying
    provenance is transitioned here from RUNNING instead. That is not a contrivance: the
    field is durable history, and once force stop can move an adopted record to ENDED it is
    exactly this rebuild that has to carry the provenance out of ORPHANED with it.
    """
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            SessionId.parse(str(session_id)),
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            SessionDisplayIdentity("opaque-editor", "claude", "recovered", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
            orphan_provenance=OrphanProvenance.ADOPTED,
        )
    )

    updated = await store.record_event(session_id, LifecycleEvent.CLEANUP_CONFIRMED)

    assert updated.orphan_provenance is OrphanProvenance.ADOPTED
    assert updated == await store.get(session_id)


async def test_a_record_pushed_into_orphaned_by_ambiguous_evidence_is_stamped_ambiguous(
    tmp_path,
) -> None:
    """The second producer. Without this stamp, NULL would mean three different things.

    `_save_trusted_orphan` is the only site that *creates* an ORPHANED record, so it is the
    only site the plan names — but the ambiguous producer reaches ORPHANED by transition
    rather than by creation, and it is `record_event` that lands it there. Stamping both
    leaves NULL meaning exactly one thing: a row written before migration 6.
    """
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
        )
    )

    updated = await store.record_event(session_id, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE)

    assert updated.state is SessionState.ORPHANED
    assert updated.orphan_provenance is OrphanProvenance.AMBIGUOUS
    reloaded = await store.get(session_id)
    assert reloaded is not None
    assert reloaded.orphan_provenance is OrphanProvenance.AMBIGUOUS


async def test_a_session_that_never_reached_orphaned_carries_no_provenance(tmp_path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
        )
    )

    updated = await store.record_event(session_id, LifecycleEvent.CLEANUP_CONFIRMED)

    assert updated.state is SessionState.ENDED
    assert updated.orphan_provenance is None


async def test_a_row_written_before_the_migration_reads_as_no_provenance(tmp_path) -> None:
    """DEC-020's default, exercised through the real upgrade rather than asserted.

    Provenance cannot be back-derived — once a pane is adopted a record exists, so the next
    reconciliation pass matches it by id and never sees an unknown pane again. So a row that
    predates the column stays NULL and takes the conservative branch.
    """
    path = tmp_path / "sessions.sqlite3"
    session_id = SessionId.new()
    old = open_database(path, migrations=MIGRATIONS[:5])
    with old:
        old.execute(
            """
            INSERT INTO sessions(
                session_id, project_id, profile_id, display_identity, state, created_at,
                resume_profile_id, resume_source_id, terminal_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(session_id),
                "opaque-editor",
                "claude",
                "opaque-editor · claude · recovered · #1",
                SessionState.ORPHANED.value,
                datetime.now(UTC).isoformat(),
                None,
                None,
                "ambiguous_terminal_evidence",
            ),
        )
    old.close()

    store = SQLiteSessionStore(open_database(path))

    reloaded = await store.get(session_id)
    assert reloaded is not None
    assert reloaded.state is SessionState.ORPHANED
    assert reloaded.orphan_provenance is None


def test_the_provenance_column_is_added_by_migration_six_and_defaults_to_null(tmp_path) -> None:
    path = tmp_path / "sessions.sqlite3"
    before = open_database(path, migrations=MIGRATIONS[:5])
    assert "orphan_provenance" not in {
        row[1] for row in before.execute("PRAGMA table_info(sessions)")
    }
    before.close()

    connection = open_database(path)

    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(sessions)")}
    assert "orphan_provenance" in columns
    # dflt_value is column 4 of PRAGMA table_info; an added column with no DEFAULT reads
    # NULL, which is what makes every pre-existing row conservative rather than adopted.
    assert columns["orphan_provenance"][4] is None


def test_the_stored_values_are_the_wire_strings_the_column_holds() -> None:
    """A StrEnum's members are what land in the column, so renaming one is a schema change."""
    assert OrphanProvenance.ADOPTED.value == "adopted"
    assert OrphanProvenance.AMBIGUOUS.value == "ambiguous"


async def test_an_unrecognised_stored_provenance_reads_as_the_conservative_branch(
    tmp_path,
) -> None:
    """A hand-edited or downgraded row must not read as the *permissive* branch.

    Falling to `None` rather than raising: `None` is the branch that offers less, so it is
    exactly as safe as refusing for a column that gates a destructive action, and it does not
    cost the caller the row. The realistic producer is a newer build writing a member this
    one does not know, then a downgrade reading it back.
    """
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(open_database(path))
    session_id = SessionId.new()
    await store.save(_adopted(session_id))
    _corrupt_provenance(path, session_id, "a-newer-builds-member")

    reloaded = await SQLiteSessionStore(open_database(path)).get(session_id)

    assert reloaded is not None
    assert reloaded.orphan_provenance is None
    assert reloaded.state is SessionState.ORPHANED


async def test_one_unreadable_provenance_does_not_cost_the_caller_every_other_session(
    tmp_path,
) -> None:
    """`list` rebuilds every row in one comprehension, so a raise here loses the whole page.

    The blast radius is what makes this worth a test rather than a comment: `list` backs the
    TUI session list, the bot's, and `ReconciliationService`'s own pass — so one bad row that
    raised would also stop reconciliation reaching every *other* session on the tick.
    """
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(open_database(path))
    healthy = [SessionId.new() for _ in range(3)]
    corrupted = SessionId.new()
    for index, session_id in enumerate([*healthy, corrupted], start=1):
        await store.save(
            SessionRecord(
                session_id,
                ProjectId("opaque-editor"),
                ProfileId("claude"),
                SessionDisplayIdentity("opaque-editor", "claude", "recovered", index),
                SessionState.ORPHANED,
                datetime.now(UTC),
                orphan_provenance=OrphanProvenance.ADOPTED,
            )
        )
    _corrupt_provenance(path, corrupted, "a-newer-builds-member")

    listed = {
        record.session_id: record for record in await SQLiteSessionStore(open_database(path)).list()
    }

    assert set(listed) == {*healthy, corrupted}
    assert [listed[session_id].orphan_provenance for session_id in healthy] == [
        OrphanProvenance.ADOPTED
    ] * 3
    assert listed[corrupted].orphan_provenance is None


def _corrupt_provenance(path, session_id: SessionId, value: str) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "UPDATE sessions SET orphan_provenance = ? WHERE session_id = ?",
            (value, str(session_id)),
        )
    connection.close()
