"""An untrusted launch survives the round trip through the real database and its reader.

Stage 1's rollback note is what this exists for. `sessions.state` is `TEXT NOT NULL` with no
CHECK, and the Python side validates on read (`SessionState(row[4])`), so a new state member
is only really a state once a row carrying its word has been written by the real store and
read back by the real reader. Until then the claim rests on fakes that never touch SQL.

The reader is the one `remote-agents doctor --history` uses: `_print_session_history` in
`bootstrap.py` calls exactly `store.get(session_id)` and `store.events(session_id)` and
renders their output. Driving the CLI itself would need `Path.home()` to be the owner's real
state directory, which a test must not touch; calling its two reads is the same evidence
without that.
"""

from __future__ import annotations

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.ports.terminal import TerminalObservation


class _TrustBlockedTerminal:
    """A terminal whose launch lands on a folder-trust dialog and stays there."""

    async def launch(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        *,
        remote_control: bool = False,
    ) -> TerminalObservation:
        del project_id, profile_id
        return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)


def _store(tmp_path) -> SQLiteSessionStore:
    return SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))


async def test_an_untrusted_launch_is_written_and_read_back_by_the_doctor_s_reader(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    service = SessionService(store, _TrustBlockedTerminal())

    record = (
        await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "trust-roundtrip")
        )
    ).record

    assert record.state is SessionState.UNTRUSTED

    # The word, as SQL actually holds it -- not as the enum renders it in this process.
    row = store._connection.execute(
        "SELECT state FROM sessions WHERE session_id = ?", (str(record.session_id),)
    ).fetchone()
    assert row[0] == "untrusted"

    # `doctor --history`'s two reads, in its order.
    read_back = await store.get(record.session_id)
    events = await store.events(record.session_id)

    assert read_back is not None
    assert read_back.state is SessionState.UNTRUSTED
    assert [event.event_type for event in events] == ["trust_required"]


async def test_the_untrusted_row_survives_a_reopen_of_the_database(tmp_path) -> None:
    """A record read back by the same process proves less than one read after a restart.

    The service that wrote the row holds it in no cache, but the connection is the same one;
    a state the schema could not really hold would still be visible across it. Reopening is
    what makes the assertion about the file.
    """
    database = tmp_path / "sessions.sqlite3"
    service = SessionService(SQLiteSessionStore(open_database(database)), _TrustBlockedTerminal())
    record = (
        await service.launch(
            LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "trust-reopen")
        )
    ).record

    reopened = SQLiteSessionStore(open_database(database))
    read_back = await reopened.get(record.session_id)
    events = await reopened.events(record.session_id)

    assert read_back is not None
    assert read_back.state is SessionState.UNTRUSTED
    # The durable history, not just the state column: `SessionState(row[4])` and the event
    # table are two separate reads of two separate schemas, and only one of them was
    # exercised above.
    assert [event.event_type for event in events] == ["trust_required"]
