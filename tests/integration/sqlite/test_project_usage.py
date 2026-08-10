"""Usage is a fact about a project's whole history, read once rather than once per row."""

from datetime import UTC, datetime, timedelta

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent

_EPOCH = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def _store(tmp_path) -> SQLiteSessionStore:
    return SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))


async def _launch(
    store: SQLiteSessionStore, project: str, *, minutes: int = 0, sequence: int = 1
) -> SessionId:
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId(project),
            ProfileId("claude"),
            SessionDisplayIdentity(project, "claude", "regular", sequence),
            SessionState.RUNNING,
            _EPOCH + timedelta(minutes=minutes),
        )
    )
    return session_id


def _by_project(usage) -> dict[str, tuple[int, datetime]]:
    return {str(entry.project_id): (entry.session_count, entry.last_used_at) for entry in usage}


async def test_usage_counts_every_session_a_project_ever_had(tmp_path) -> None:
    """Including the ones that have ENDED, which is most of them.

    A project earns its rank through launches that are over. Counting only live sessions would
    make a heavily used project look untouched the moment its terminals were cleaned up, which
    is exactly the state a long-lived catalogue is in.
    """
    store = _store(tmp_path)
    first = await _launch(store, "opaque-editor", minutes=0, sequence=1)
    await _launch(store, "opaque-editor", minutes=30, sequence=2)
    await store.record_event(first, LifecycleEvent.CLEANUP_CONFIRMED)
    assert (await store.get(first)).state is SessionState.ENDED

    usage = _by_project(await store.project_usage())

    assert usage["opaque-editor"] == (2, _EPOCH + timedelta(minutes=30))


async def test_last_used_at_is_the_most_recent_launch_not_the_last_row_written(tmp_path) -> None:
    """The rows are written out of order on purpose: MAX has to beat insertion order."""
    store = _store(tmp_path)
    await _launch(store, "opaque-editor", minutes=120, sequence=1)
    await _launch(store, "opaque-editor", minutes=5, sequence=2)

    usage = _by_project(await store.project_usage())

    assert usage["opaque-editor"] == (2, _EPOCH + timedelta(minutes=120))


async def test_each_project_is_summarized_separately(tmp_path) -> None:
    """One group per project, so one busy project cannot inflate a quiet one."""
    store = _store(tmp_path)
    await _launch(store, "opaque-editor", minutes=0, sequence=1)
    await _launch(store, "opaque-editor", minutes=10, sequence=2)
    await _launch(store, "ledger-cli", minutes=45, sequence=1)

    usage = _by_project(await store.project_usage())

    assert usage == {
        "ledger-cli": (1, _EPOCH + timedelta(minutes=45)),
        "opaque-editor": (2, _EPOCH + timedelta(minutes=10)),
    }


async def test_a_project_with_no_sessions_is_absent_rather_than_zero(tmp_path) -> None:
    """The sessions table cannot name a project it has never seen, and must not pretend to.

    A zero-count entry would be an assertion that the project exists, which only the catalogue
    knows. Absence lets the ranking supply its own default for everything it holds, instead of
    trusting a list that could only ever be partial.
    """
    store = _store(tmp_path)
    await _launch(store, "opaque-editor")

    usage = await store.project_usage()

    assert [str(entry.project_id) for entry in usage] == ["opaque-editor"]
    assert all(entry.session_count > 0 for entry in usage)


async def test_an_empty_store_reports_no_usage_at_all(tmp_path) -> None:
    """Not an error and not a row of zeroes: a first run simply has nothing to rank by."""
    store = _store(tmp_path)

    assert await store.project_usage() == ()


async def test_last_used_at_comes_back_timezone_aware(tmp_path) -> None:
    """A ranking subtracts this from a clock, and a naive value would raise where it is used."""
    store = _store(tmp_path)
    await _launch(store, "opaque-editor", minutes=90)

    (entry,) = await store.project_usage()

    assert entry.last_used_at.tzinfo is not None
    assert entry.last_used_at.utcoffset() == timedelta(0)
    assert datetime.now(UTC) - entry.last_used_at > timedelta(0)


async def test_a_stored_timestamp_without_an_offset_is_read_as_utc(tmp_path) -> None:
    """Everything `save` writes is UTC, so an offset-less row is old, not local.

    Reading it in the machine's zone would move a session by hours on a developer's laptop and
    not at all on a UTC server, which is the kind of difference that never shows up in a test
    written in one of those two places.
    """
    store = _store(tmp_path)
    await _launch(store, "opaque-editor", minutes=15)
    store._connection.execute(
        "UPDATE sessions SET created_at = ? WHERE project_id = ?",
        ("2026-08-10T09:15:00", "opaque-editor"),
    )
    store._connection.commit()

    (entry,) = await store.project_usage()

    assert entry.last_used_at == _EPOCH + timedelta(minutes=15)


async def test_usage_is_one_statement_however_many_projects_there_are(tmp_path) -> None:
    """The claim is about statement count, so it is counted rather than asserted in prose.

    The per-row shape of this feature — count the sessions of the project you are about to
    render — costs a statement per row of every refresh. Growing the catalogue from three
    projects to thirty and watching the number of executed statements stay at one is what
    separates the two implementations; a single-project test would not.

    This checks the store's side only. That a *caller* asks once per refresh instead of once
    per rendered row is a fact about the caller, and cannot be observed from here.
    """
    store = _store(tmp_path)
    for index in range(3):
        await _launch(store, f"small-{index}")
    small_statements: list[str] = []
    store._connection.set_trace_callback(small_statements.append)
    await store.project_usage()
    store._connection.set_trace_callback(None)

    for index in range(30):
        await _launch(store, f"large-{index}", minutes=index)
    large_statements: list[str] = []
    store._connection.set_trace_callback(large_statements.append)
    usage = await store.project_usage()
    store._connection.set_trace_callback(None)

    assert len(usage) == 33
    assert len(small_statements) == 1, small_statements
    assert large_statements == small_statements
