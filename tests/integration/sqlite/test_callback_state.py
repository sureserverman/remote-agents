import threading

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


def test_concurrent_connections_cannot_both_claim_one_mutation(tmp_path) -> None:
    """The atomicity property for the claim, which the sequential test above cannot reach.

    Its sibling proves the claim is not a Python attribute; it cannot distinguish an atomic
    `UPDATE … WHERE claimed = 0` from `if not claimed(): mark_claimed()`, because nothing
    ever interleaves. Here the connections are released together by a barrier so their
    read-then-write windows genuinely overlap — which is what DEC-005's second writer does.
    Mirrors `test_chat_view.py`'s adopt race; each thread opens its own connection, since a
    `sqlite3` connection belongs to the thread that made it.

    Two claims both returning True is a stop or a launch serviced twice, which is the exact
    guarantee DEC-008 rests on.
    """
    path = tmp_path / "sessions.sqlite3"
    minting = SQLiteCallbackStateStore(open_database(path))
    token = minting.create("launch.profile", "p|claude", _OWNER, _CHAT, _MESSAGE, mutation=True)
    scope = {"owner_id": _OWNER, "chat_id": _CHAT, "message_id": _MESSAGE}
    claimants = 8
    ready = threading.Barrier(claimants)
    won: list[bool] = []
    guard = threading.Lock()

    def claim() -> None:
        store = SQLiteCallbackStateStore(open_database(path))
        ready.wait(timeout=10)
        outcome = store.claim_mutation(token, **scope)
        with guard:
            won.append(outcome)

    threads = [threading.Thread(target=claim) for _ in range(claimants)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert won.count(True) == 1, (
        "two connections both told they claimed the mutation is a double-executed action"
    )
    assert len(won) == claimants


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


def test_a_message_that_is_gone_takes_exactly_its_own_tokens_with_it(tmp_path) -> None:
    """The retention rule that replaced the TTL: tokens die with their screen, not on a clock."""
    store = _store(tmp_path)
    doomed = [store.create("nav.home", "home", _OWNER, _CHAT, _MESSAGE) for _ in range(3)]
    survivor = store.create("nav.home", "home", _OWNER, _CHAT, _MESSAGE + 1)
    other_chat = store.create("nav.home", "home", _OWNER, _CHAT + 1, _MESSAGE)

    assert store.prune_for_message(_CHAT, _MESSAGE) == 3

    for token in doomed:
        assert store.resolve(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is None
    assert store.resolve(survivor, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE + 1)
    assert store.resolve(other_chat, owner_id=_OWNER, chat_id=_CHAT + 1, message_id=_MESSAGE)
    assert store.active_count() == 2


def test_capacity_is_bounded_by_size_now_that_it_is_not_bounded_by_time(tmp_path, caplog) -> None:
    """Without a TTL nothing else reaps the table, so the cap is the only bound left.

    The oldest go rather than the newest being refused: a refusal renders a keyboard whose
    buttons were never created, which is the failure the previous store actually had.
    """
    limit = 20
    store = _store(tmp_path, limit=limit)
    tokens = [store.create("nav.home", "home", _OWNER, _CHAT, _MESSAGE) for _ in range(limit + 5)]

    assert store.active_count() <= limit
    assert all(
        store.resolve(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is not None
        for token in tokens[-limit:]
    )
    assert store.resolve(tokens[0], owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is None


def test_an_eviction_pass_logs_once_however_many_tokens_it_discards(tmp_path, caplog) -> None:
    """Creating one at a time evicts one at a time, which cannot tell the two shapes apart.

    A steady trickle logs once per pass *and* once per token — the same count either way — so
    a regression moving the log inside the delete loop would pass a test written that way.
    The table is therefore pushed several rows past capacity behind the store's back, and the
    one `create` that notices has to answer for all of them in a single line.
    """
    limit = 10
    store = _store(tmp_path, limit=limit)
    connection = open_database(tmp_path / "sessions.sqlite3")
    with connection:
        connection.executemany(
            "INSERT INTO callback_states(token, action, entity_id, owner_id, chat_id, "
            "message_id, mutation, claimed, created_at) VALUES (?, 'nav.home', 'home', ?, ?, "
            "?, 0, 0, '2026-01-01T00:00:00+00:00')",
            [(f"c1_seed{index}", _OWNER, _CHAT, _MESSAGE) for index in range(limit + 4)],
        )

    with caplog.at_level("INFO"):
        store.create("nav.home", "home", _OWNER, _CHAT, _MESSAGE)

    evictions = [record for record in caplog.records if "evicted" in record.message]
    assert len(evictions) == 1, "one pass discarding five tokens must not log five times"
    assert "evicted 5 " in evictions[0].message
    assert store.active_count() == limit


def test_binding_leaves_another_chats_pending_tokens_alone(tmp_path) -> None:
    """Parity with the in-memory store, which had this test and the durable one did not.

    A dropped `WHERE chat_id = ?` would let one chat's undelivered buttons be adopted by
    another chat's message, and nothing in this suite would have noticed.
    """
    store = _store(tmp_path)
    mine = store.create("nav.home", "home", _OWNER, _CHAT)
    theirs = store.create("nav.home", "home", _OWNER, _CHAT + 1)

    assert store.bind_pending(_CHAT, _MESSAGE) == 1

    assert store.resolve(mine, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is not None
    assert store.resolve(theirs, owner_id=_OWNER, chat_id=_CHAT + 1, message_id=_MESSAGE) is None


def test_a_moved_screen_carries_its_tokens_across_a_restart(tmp_path) -> None:
    """The durable half of the move, which is the half that matters.

    The live view is re-sent below arriving notifications, so its keyboard changes message
    while the owner is not looking. A rebind that lived only in memory would leave every button
    on the moved screen dead after the next restart — the exact defect sub-plan 1 made the
    store durable to remove.
    """
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteCallbackStateStore(connection)
    token = store.create("sessions.open", "sessions", _OWNER, _CHAT, _MESSAGE)

    assert store.rebind(_CHAT, _MESSAGE, _MESSAGE + 5) == 1
    connection.close()

    reopened = SQLiteCallbackStateStore(open_database(tmp_path / "sessions.sqlite3"))
    assert reopened.resolve(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE) is None
    assert (
        reopened.resolve(token, owner_id=_OWNER, chat_id=_CHAT, message_id=_MESSAGE + 5) is not None
    )


def test_a_claim_survives_the_live_view_moving_below_a_notification(tmp_path) -> None:
    """The window: `resolve` succeeds, a notification moves the view, then the claim runs.

    `ActivityNotifier.deliver` calls `LiveView.move_to_bottom` once per pass, which `rebind`s
    the anchor's tokens onto the re-sent message. A mutating press is resolved against the
    message it came from, then — for every action that shows a pending screen — waits a
    Telegram round trip before claiming. A rebind inside that gap used to make the claim match
    no row, so a launch, stop or resume that never ran answered "That action has already run".

    That is the *rebind working as designed* breaking the claim: tokens are moved precisely so
    the keyboard keeps resolving across the move, and the claim was the one thing that did not.
    """
    store = SQLiteCallbackStateStore(open_database(tmp_path / "sessions.sqlite3"))
    token = store.create("launch.profile", "project|claude", 7, 11, mutation=True)
    store.bind_pending(11, 100)

    assert store.resolve(token, owner_id=7, chat_id=11, message_id=100) is not None
    store.rebind(11, 100, 200)

    assert store.claim_mutation(token, owner_id=7, chat_id=11, message_id=100) is True


def test_the_one_shot_still_admits_exactly_one_caller_across_a_rebind(tmp_path) -> None:
    """The claim stops re-checking the message; it does not stop being a one-shot. DEC-008's
    "a destructive action drops a repeat" is the property that must survive this change."""
    store = SQLiteCallbackStateStore(open_database(tmp_path / "sessions.sqlite3"))
    token = store.create("graceful", "session:claude", 7, 11, mutation=True)
    store.bind_pending(11, 100)
    store.rebind(11, 100, 200)

    first = store.claim_mutation(token, owner_id=7, chat_id=11, message_id=100)
    second = store.claim_mutation(token, owner_id=7, chat_id=11, message_id=200)

    assert (first, second) == (True, False), "one caller, whichever message it names"


def test_a_claim_still_refuses_another_owner_or_chat(tmp_path) -> None:
    """Owner and chat stay in the claim. They never change under a rebind, so keeping them
    costs nothing and they are the half that is about authorization rather than about which
    message is currently on screen."""
    store = SQLiteCallbackStateStore(open_database(tmp_path / "sessions.sqlite3"))
    token = store.create("launch.profile", "project|claude", 7, 11, mutation=True)
    store.bind_pending(11, 100)

    assert store.claim_mutation(token, owner_id=8, chat_id=11, message_id=100) is False
    assert store.claim_mutation(token, owner_id=7, chat_id=12, message_id=100) is False
    assert store.claim_mutation(token, owner_id=7, chat_id=11, message_id=100) is True
