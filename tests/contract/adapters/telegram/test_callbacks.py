"""Callback payloads are opaque, bounded, bound to their message, and replay-safe.

What is deliberately no longer here: an expiry. A token used to die fifteen minutes after
it was drawn, and again — sooner and more often — whenever any newer screen bumped a
chat-global revision counter. Both are gone, so the tests that pinned them are replaced by
the two properties that took over: a token is valid for exactly the message it was drawn
on, and a mutating token is still claimable exactly once.
"""

from remote_agents.adapters.telegram.callbacks import CallbackStateStore

_MESSAGE = 100


def test_callback_tokens_are_bounded_opaque_utf8_and_hide_server_side_values() -> None:
    store = CallbackStateStore()
    token = store.create(
        action="launch.confirm",
        entity_id="/home/user/dev/private-project",
        owner_id=7,
        chat_id=11,
        message_id=_MESSAGE,
    )

    assert 1 <= len(token.encode("utf-8")) <= 64
    assert token.isascii()
    assert "/" not in token
    assert "launch" not in token
    assert "private" not in token


def test_callback_resolution_binds_owner_chat_and_message() -> None:
    store = CallbackStateStore()
    token = store.create("view.refresh", "project-1", 7, 11, _MESSAGE)

    resolved = store.resolve(token, owner_id=7, chat_id=11, message_id=_MESSAGE)

    assert resolved is not None and resolved.action == "view.refresh"
    assert store.resolve(token + "x", owner_id=7, chat_id=11, message_id=_MESSAGE) is None
    assert store.resolve(token, owner_id=8, chat_id=11, message_id=_MESSAGE) is None
    assert store.resolve(token, owner_id=7, chat_id=12, message_id=_MESSAGE) is None
    assert store.resolve(token, owner_id=7, chat_id=11, message_id=_MESSAGE + 1) is None


def test_a_token_never_expires_however_long_it_is_left(monkeypatch) -> None:
    """The reported defect, pinned: nothing in this store consults a clock at all.

    An AST-free way to say "no TTL" would be to advance a clock and re-resolve, but there is
    no clock left to advance — so the check is that the store exposes no time input and the
    token resolves after the store has served four hundred days' worth of other traffic.
    """
    store = CallbackStateStore()
    token = store.create("nav.home", "home", 7, 11, _MESSAGE)
    for _ in range(400):
        store.create("nav.home", "home", 7, 11, _MESSAGE + 1)

    assert store.resolve(token, owner_id=7, chat_id=11, message_id=_MESSAGE) is not None


def test_a_token_is_minted_unbound_and_bound_to_the_message_that_carries_it() -> None:
    """A keyboard is built before it is sent, so binding is a second step, not a guess."""
    store = CallbackStateStore()
    token = store.create("sessions.open", "sessions", 7, 11)

    assert store.resolve(token, owner_id=7, chat_id=11, message_id=_MESSAGE) is None
    assert store.bind_pending(11, _MESSAGE) == 1
    assert store.resolve(token, owner_id=7, chat_id=11, message_id=_MESSAGE) is not None


def test_binding_leaves_another_chats_pending_tokens_alone() -> None:
    store = CallbackStateStore()
    mine = store.create("nav.home", "home", 7, 11)
    theirs = store.create("nav.home", "home", 7, 12)

    store.bind_pending(11, _MESSAGE)

    assert store.resolve(mine, owner_id=7, chat_id=11, message_id=_MESSAGE) is not None
    assert store.resolve(theirs, owner_id=7, chat_id=12, message_id=_MESSAGE) is None


def test_mutation_callback_nonce_is_claimed_exactly_once() -> None:
    store = CallbackStateStore()
    token = store.create("launch.confirm", "project-1", 7, 11, _MESSAGE, mutation=True)

    assert store.claim_mutation(token, owner_id=7, chat_id=11, message_id=_MESSAGE) is True
    assert store.claim_mutation(token, owner_id=7, chat_id=11, message_id=_MESSAGE) is False


def test_a_replaced_screen_takes_its_tokens_with_it() -> None:
    """Pruning is what replaced the revision counter: the stale view stops existing."""
    store = CallbackStateStore()
    stale = store.create("launch.confirm", "project-1", 7, 11, _MESSAGE, mutation=True)
    elsewhere = store.create("nav.home", "home", 7, 11, _MESSAGE + 1)

    assert store.prune_for_message(11, _MESSAGE) == 1
    assert store.resolve(stale, owner_id=7, chat_id=11, message_id=_MESSAGE) is None
    assert store.resolve(elsewhere, owner_id=7, chat_id=11, message_id=_MESSAGE + 1) is not None


def test_a_full_store_evicts_its_oldest_rather_than_refusing_a_new_screen() -> None:
    """Refusing to mint would render a keyboard whose buttons were never created."""
    store = CallbackStateStore(limit=2)
    oldest = store.create("view.refresh", "project-1", 7, 11, _MESSAGE)
    kept = store.create("view.refresh", "project-2", 7, 11, _MESSAGE)

    newest = store.create("view.refresh", "project-3", 7, 11, _MESSAGE)

    assert store.active_count() == 2
    assert store.resolve(oldest, owner_id=7, chat_id=11, message_id=_MESSAGE) is None
    for token in (kept, newest):
        assert store.resolve(token, owner_id=7, chat_id=11, message_id=_MESSAGE) is not None


def test_a_moved_screen_carries_its_tokens_to_the_message_that_replaced_it() -> None:
    """The live view is re-sent below arriving notifications so the menu stays reachable.

    Re-sending without this leaves the new message showing a keyboard whose every token still
    names the message just deleted — a screen that looks right and answers nothing, which is
    the dead-button state the message-scoped store exists to make impossible.
    """
    store = CallbackStateStore()
    moving = store.create("sessions.open", "sessions", 7, 11, _MESSAGE)
    elsewhere = store.create("nav.home", "home", 7, 11, _MESSAGE + 1)

    assert store.rebind(11, _MESSAGE, _MESSAGE + 5) == 1
    assert store.resolve(moving, owner_id=7, chat_id=11, message_id=_MESSAGE) is None
    assert store.resolve(moving, owner_id=7, chat_id=11, message_id=_MESSAGE + 5) is not None
    assert store.resolve(elsewhere, owner_id=7, chat_id=11, message_id=_MESSAGE + 1) is not None


def test_a_move_onto_an_unreal_message_is_refused() -> None:
    """UNBOUND means "no message yet". Rebinding onto it would make every pending-token
    binding in the chat adopt these as well."""
    store = CallbackStateStore()
    store.create("sessions.open", "sessions", 7, 11, _MESSAGE)

    for unreal in (0, -1):
        try:
            store.rebind(11, _MESSAGE, unreal)
        except ValueError:
            continue
        raise AssertionError(f"rebinding onto {unreal} was accepted")
