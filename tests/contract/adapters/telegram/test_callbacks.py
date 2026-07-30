"""Callback payloads are opaque, bounded, bound to a view, and replay-safe."""

from datetime import UTC, datetime, timedelta

from remote_agents.adapters.telegram.callbacks import CallbackStateStore


def test_callback_tokens_are_bounded_opaque_utf8_and_hide_server_side_values() -> None:
    store = CallbackStateStore(now=lambda: datetime(2026, 7, 30, tzinfo=UTC))
    token = store.create(
        action="launch.confirm",
        entity_id="/home/user/dev/private-project",
        owner_id=7,
        chat_id=11,
        view_revision=3,
    )

    assert 1 <= len(token.encode("utf-8")) <= 64
    assert token.isascii()
    assert "/" not in token
    assert "launch" not in token
    assert "private" not in token


def test_callback_resolution_binds_owner_chat_revision_and_expiry() -> None:
    current = datetime(2026, 7, 30, tzinfo=UTC)
    clock = [current]
    store = CallbackStateStore(now=lambda: clock[0], ttl=timedelta(minutes=1))
    token = store.create("view.refresh", "project-1", 7, 11, 3)

    assert store.resolve(token, owner_id=7, chat_id=11, view_revision=3).action == "view.refresh"
    assert store.resolve(token + "x", owner_id=7, chat_id=11, view_revision=3) is None
    assert store.resolve(token, owner_id=8, chat_id=11, view_revision=3) is None
    assert store.resolve(token, owner_id=7, chat_id=12, view_revision=3) is None
    assert store.resolve(token, owner_id=7, chat_id=11, view_revision=4) is None

    clock[0] += timedelta(minutes=2)
    assert store.resolve(token, owner_id=7, chat_id=11, view_revision=3) is None


def test_mutation_callback_nonce_is_claimed_exactly_once() -> None:
    store = CallbackStateStore(now=lambda: datetime(2026, 7, 30, tzinfo=UTC))
    token = store.create("launch.confirm", "project-1", 7, 11, 3, mutation=True)

    assert store.claim_mutation(token, owner_id=7, chat_id=11, view_revision=3) is True
    assert store.claim_mutation(token, owner_id=7, chat_id=11, view_revision=3) is False
