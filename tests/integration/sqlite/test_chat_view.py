"""The anchor is durable, so a restart redraws the live view instead of adding one."""

import threading

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


def test_adopting_claims_an_absent_anchor_and_refuses_a_present_one(tmp_path) -> None:
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))

    assert store.adopt_anchor(_CHAT, 100) is True
    assert store.adopt_anchor(_CHAT, 205) is False
    assert store.anchor(_CHAT) == 100


def test_a_sequence_of_writers_leaves_the_first_anchor_standing(tmp_path) -> None:
    """Sequential, and says so: this proves durability across connections and that a later
    writer cannot move the anchor — **not** atomicity, which needs the interleaving below.

    Two real connections, not two stores over one handle: sharing a connection would only
    prove the guard is not a Python attribute.
    """
    database = tmp_path / "sessions.sqlite3"
    writers = [SQLiteChatViewStore(open_database(database)) for _ in range(20)]

    won = [store.adopt_anchor(_CHAT, 100 + index) for index, store in enumerate(writers)]

    assert won == [True] + [False] * 19
    assert SQLiteChatViewStore(open_database(database)).anchor(_CHAT) == 100


def test_concurrent_writers_cannot_both_adopt_the_same_chat_s_first_anchor(tmp_path) -> None:
    """The atomicity property, and the only test here that can fail if it is lost.

    DEC-005 permits a second process writing this store, so the conditional cannot live
    between two of our round trips. The writers are released together by a barrier so their
    read-then-write windows genuinely overlap — a sequential loop cannot distinguish an
    atomic `ON CONFLICT DO NOTHING` from `if anchor() is None: record_anchor()`, because
    nothing ever interleaves. Each thread opens its own connection, since a `sqlite3`
    connection belongs to the thread that made it.
    """
    database = tmp_path / "sessions.sqlite3"
    # Migrate once up front so the threads race the adopt rather than the schema.
    open_database(database).close()
    writers = 8
    ready = threading.Barrier(writers)
    won: list[bool] = []
    guard = threading.Lock()

    def adopt(index: int) -> None:
        store = SQLiteChatViewStore(open_database(database))
        ready.wait(timeout=10)
        outcome = store.adopt_anchor(_CHAT, 100 + index)
        with guard:
            won.append(outcome)

    threads = [threading.Thread(target=adopt, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert won.count(True) == 1, (
        "two writers both told they set the anchor is the read-then-write defect itself"
    )
    anchor = SQLiteChatViewStore(open_database(database)).anchor(_CHAT)
    assert anchor in range(100, 100 + writers)


def test_adopting_answers_false_even_for_the_anchor_already_stored(tmp_path) -> None:
    """True means *this call inserted it* — the only answer SQLite can actually give.

    `ON CONFLICT DO NOTHING` reports whether a row was written, not which value it
    conflicted with, so a port that promised "true if the anchor is now yours" would be
    promising something one of its two implementations cannot honour.
    """
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))
    store.adopt_anchor(_CHAT, 100)

    assert store.adopt_anchor(_CHAT, 100) is False
    assert store.anchor(_CHAT) == 100


@pytest.mark.parametrize("message_id", [0, -1])
def test_adopting_the_unbound_sentinel_is_refused_too(tmp_path, message_id: int) -> None:
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))

    with pytest.raises(ValueError):
        store.adopt_anchor(_CHAT, message_id)


@pytest.mark.parametrize("message_id", [0, -1])
def test_the_unbound_sentinel_is_refused_as_an_anchor(tmp_path, message_id: int) -> None:
    """Zero is `UNBOUND`. Anchoring the live view to it would address a message that does
    not exist, and every later render would edit into nothing."""
    store = SQLiteChatViewStore(open_database(tmp_path / "sessions.sqlite3"))

    with pytest.raises(ValueError):
        store.record_anchor(_CHAT, message_id)
