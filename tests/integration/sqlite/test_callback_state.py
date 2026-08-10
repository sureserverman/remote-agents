from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.database import open_database

_OWNER = 7
_CHAT = 11
_MESSAGE = 100


def _store(tmp_path, **arguments) -> SQLiteCallbackStateStore:
    return SQLiteCallbackStateStore(open_database(tmp_path / "sessions.sqlite3"), **arguments)


def test_a_token_resolves_through_a_store_that_did_not_mint_it(tmp_path) -> None:
    """A restart replaces the store object, not the database — the button must survive it."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    token = SQLiteCallbackStateStore(connection).create(
        "sessions.open", "sessions", _OWNER, _CHAT, _MESSAGE
    )

    resolved = SQLiteCallbackStateStore(connection).resolve(
        token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE
    )

    assert resolved is not None
    assert (resolved.action, resolved.entity_id) == ("sessions.open", "sessions")


def test_age_is_never_a_reason_to_refuse_a_token(tmp_path) -> None:
    """The whole point of the change: four hundred days old is still a working button."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteCallbackStateStore(connection)
    token = store.create("nav.home", "home", _OWNER, _CHAT, _MESSAGE)
    with connection:
        connection.execute(
            "UPDATE callback_states SET created_at = '2025-01-01T00:00:00+00:00' WHERE token = ?",
            (token,),
        )

    assert store.resolve(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is not None


def test_a_token_belongs_to_the_message_it_was_drawn_on(tmp_path) -> None:
    store = _store(tmp_path)
    token = store.create("nav.home", "home", _OWNER, _CHAT, _MESSAGE)

    assert store.resolve(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE + 1) is None
    assert store.resolve(token, owner_id=_OWNER + 1, chat_id=_CHAT, message_id=_MESSAGE) is None
    assert store.resolve(token, owner_id=_OWNER, chat_id=_CHAT + 1, message_id=_MESSAGE) is None
    assert store.resolve("c1_absent", owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is None


def test_a_mutation_is_claimed_once_across_two_connections_to_one_database(tmp_path) -> None:
    """DEC-005 permits a second writer, so the claim has to be atomic rather than in memory.

    Two separate connections rather than two stores over one: sharing a connection would
    prove only that the claim is not a Python attribute, and the claim this method actually
    makes is about the other *process* DEC-005 allows.
    """
    path = tmp_path / "sessions.sqlite3"
    minting = SQLiteCallbackStateStore(open_database(path))
    competing = SQLiteCallbackStateStore(open_database(path))
    token = minting.create("launch.profile", "p|claude", _OWNER, _CHAT, _MESSAGE, mutation=True)
    scope = {"owner_id": _OWNER, "chat_id": _CHAT, "message_id": _MESSAGE}

    assert minting.claim_mutation(token, **scope) is True
    assert competing.claim_mutation(token, **scope) is False
    assert competing.resolve(token, **scope) is not None


def test_a_read_only_token_can_never_be_claimed(tmp_path) -> None:
    store = _store(tmp_path)
    token = store.create("sessions.open", "sessions", _OWNER, _CHAT, _MESSAGE)

    assert store.claim_mutation(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is False


def test_creating_a_token_rejects_an_unsafe_descriptor(tmp_path) -> None:
    store = _store(tmp_path)

    unsafe = (("", "home", 1), ("nav.home", "", 1), ("nav.home", "h", -1))
    for action, entity_id, message_id in unsafe:
        try:
            store.create(action, entity_id, _OWNER, _CHAT, message_id)
        except ValueError:
            continue
        raise AssertionError(f"accepted an unsafe callback descriptor: {action!r} {entity_id!r}")
