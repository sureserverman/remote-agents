from datetime import UTC, datetime

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


async def test_resume_identity_is_durable_and_unique(tmp_path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
        SessionState.STARTING,
        datetime.now(UTC),
        ProfileId("claude"),
        "source-123",
    )
    await store.save(record)

    assert await store.get_by_resume_source(ProfileId("claude"), "source-123") == record
