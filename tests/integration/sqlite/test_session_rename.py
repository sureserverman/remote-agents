"""A session can be named after it exists, by rewriting the identity it already stores."""

from datetime import UTC, datetime

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    MAX_LABEL_LENGTH,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_SESSION = SessionId.new()


def _store(tmp_path) -> SQLiteSessionStore:
    return SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))


def _record(label: str | None = None) -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1, label),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _stored_identity(store: SQLiteSessionStore) -> str:
    row = store._connection.execute(
        "SELECT display_identity FROM sessions WHERE session_id = ?", (str(_SESSION),)
    ).fetchone()
    return str(row[0])


async def test_naming_an_unnamed_session_produces_a_row_that_reads_back(tmp_path) -> None:
    """A 4-part identity becomes a 5-part one, and `_record_from_row` has to accept it.

    The label is the optional fifth part of one column, so the write and the read are the same
    format agreeing — assert on the stored string as well as the returned record, or a store
    that returned the right object while writing an unparseable row would pass.
    """
    store = _store(tmp_path)
    await store.save(_record())
    assert _stored_identity(store) == "opaque-editor · claude · regular · #1"

    updated = await store.set_label(_SESSION, "release review")

    assert updated.display.custom_label == "release review"
    assert _stored_identity(store) == "opaque-editor · claude · regular · #1 · release review"
    reread = await store.get(_SESSION)
    assert reread is not None
    assert reread.display.custom_label == "release review"


async def test_renaming_replaces_the_label_rather_than_appending_one(tmp_path) -> None:
    """Five parts stay five. A second label appended would make a six-part row."""
    store = _store(tmp_path)
    await store.save(_record("first"))

    await store.set_label(_SESSION, "second")

    assert _stored_identity(store) == "opaque-editor · claude · regular · #1 · second"
    reread = await store.get(_SESSION)
    assert reread is not None and reread.display.custom_label == "second"


async def test_clearing_a_label_returns_the_row_to_four_parts(tmp_path) -> None:
    """`None` removes the name rather than storing an empty one.

    An empty fifth part would read back as a five-part identity carrying a blank label, which
    renders as a trailing separator the owner never asked for.
    """
    store = _store(tmp_path)
    await store.save(_record("temporary"))

    updated = await store.set_label(_SESSION, None)

    assert updated.display.custom_label is None
    assert _stored_identity(store) == "opaque-editor · claude · regular · #1"


@pytest.mark.parametrize(
    ("label", "why"),
    [
        ("x" * (MAX_LABEL_LENGTH + 1), "one character over the bound"),
        ("bad\x07label", "a non-printable character"),
        ("", "empty"),
        ("   ", "whitespace that normalizes to empty"),
    ],
)
async def test_an_invalid_label_is_refused_without_writing(tmp_path, label, why) -> None:
    """Rejected before the UPDATE, so a refused rename leaves the row exactly as it was.

    Validating after the write, or in the caller only, would leave the store the one component
    that can hold a name the domain would refuse to construct.
    """
    store = _store(tmp_path)
    await store.save(_record("kept"))

    with pytest.raises(ValueError):
        await store.set_label(_SESSION, label)

    assert _stored_identity(store) == "opaque-editor · claude · regular · #1 · kept", why


async def test_a_label_is_normalized_to_the_form_it_will_be_compared_in(tmp_path) -> None:
    """Collapsed whitespace, so two names that read identically are stored identically."""
    store = _store(tmp_path)
    await store.save(_record())

    updated = await store.set_label(_SESSION, "  release   review  ")

    assert updated.display.custom_label == "release review"
    assert _stored_identity(store).endswith("· release review")


async def test_renaming_a_session_that_is_not_stored_raises(tmp_path) -> None:
    """Answering "renamed" about nothing would let a stale button report success.

    The same fail-dangerous default the stop path removed: a missing row is a fact worth
    raising on, not a no-op to report as done.
    """
    store = _store(tmp_path)

    with pytest.raises(KeyError):
        await store.set_label(SessionId.new(), "anything")


async def test_a_session_renamed_from_the_bot_reads_back_named_on_the_local_surface(
    tmp_path,
) -> None:
    """DEC-005 has two processes over one store, so a rename has to be a store fact.

    The bot renames; the TUI is a different process reading the same rows and renders
    `session_row`, which is built from the identity the store returns. Nothing is pushed
    between the surfaces and nothing needs to be — but that is a claim about a join, and this
    is where it is checked rather than assumed.
    """
    from remote_agents.adapters.tui.model import session_row

    store = _store(tmp_path)
    await store.save(_record())
    assert "release review" not in session_row((await store.list())[0])

    await store.set_label(_SESSION, "release review")

    reopened = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    assert session_row((await reopened.list())[0]).startswith(
        "opaque-editor · claude · regular · #1 · release review · running · "
    )
