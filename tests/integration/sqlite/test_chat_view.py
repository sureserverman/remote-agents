"""The anchor is durable, so a restart redraws the live view instead of adding one."""

import pytest

from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.database import open_database

_CHAT = 11


def test_a_chat_with_no_live_view_yet_has_no_anchor(tmp_path) -> None:
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))

    assert store.anchor(_CHAT) is None


def test_an_anchor_survives_the_connection_that_recorded_it(tmp_path) -> None:
    """The restart property. Composed exactly as `bootstrap.main` does it: the first
    connection is closed before the second is opened, so nothing is proved by a shared
    handle to an already-open file."""
    database = tmp_path / "sessions.sqlite3"
    first = open_database(database)
    SQLiteChatViewStore(first).record_anchor(_CHAT, 100)
    first.close()

    assert SQLiteChatViewStore(open_database(database)).anchor(_CHAT) == 100


def test_recording_again_moves_the_anchor_rather_than_keeping_two(tmp_path) -> None:
    """One chat, one live view — the primary key is the invariant, not a convenience."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteChatViewStore(connection)
    store.record_anchor(_CHAT, 100)

    store.record_anchor(_CHAT, 205)

    assert store.anchor(_CHAT) == 205
    assert connection.execute("SELECT COUNT(*) FROM chat_views").fetchone()[0] == 1


def test_two_chats_keep_their_own_anchors(tmp_path) -> None:
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))
    store.record_anchor(_CHAT, 100)
    store.record_anchor(_CHAT + 1, 400)

    assert (store.anchor(_CHAT), store.anchor(_CHAT + 1)) == (100, 400)


@pytest.mark.parametrize("message_id", [0, -1])
def test_the_unbound_sentinel_is_refused_as_an_anchor(tmp_path, message_id: int) -> None:
    """Zero is `UNBOUND`. Anchoring the live view to it would address a message that does
    not exist, and every later render would edit into nothing."""
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))

    with pytest.raises(ValueError):
        store.record_anchor(_CHAT, message_id)
