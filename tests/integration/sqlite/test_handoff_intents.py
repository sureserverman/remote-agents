from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.external_sessions import ExternalProcessIdentity
from remote_agents.domain.handoff_intents import HandoffIntent, HandoffState
from remote_agents.domain.models import ProfileId, ProjectId


async def test_handoff_intent_is_durable_and_state_updates_are_idempotent(tmp_path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    intent = HandoffIntent(
        "h-1",
        ProfileId("claude"),
        ProjectId("opaque-editor"),
        "source-1",
        ExternalProcessIdentity(42, 9, 1000, "claude"),
        HandoffState.REQUESTED,
    )
    await store.save_handoff_intent(intent)

    updated = await store.update_handoff_state(intent.intent_id, HandoffState.STOP_SENT)

    assert updated.state is HandoffState.STOP_SENT
