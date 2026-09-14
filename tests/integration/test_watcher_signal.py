"""What the change watcher now says, and what it no longer says.

This is the claim the whole store split exists to be able to make. `StoreWatch` fingerprints a
file and publishes `StoreChanged`; every surface re-reads on that signal. While the Telegram
surface's own bookkeeping lived in that file, a keyboard being minted was indistinguishable from
a session changing — so an open sessions page redrew, minted, republished its own change and
redrew again, about thirty real edits a minute into one private chat, until Telegram flood-banned
the bot for six hours.

Both halves are asserted here, and both are needed. A watcher that published nothing would pass
the first test perfectly, and one that published on everything would pass the second.

Driven through `poll_once` rather than by sleeping past the one-second interval: the question is
whether a poll *finds* a change, and asking it directly is both exact and fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import (
    open_database,
    open_ui_database,
    ui_database_path,
    watched_paths,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.store_watch import StoreWatch


def _composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from remote_agents.bootstrap import _private_boundary
    from remote_agents.config import AppConfig, load_secrets
    from remote_agents.production import ProductionPaths

    monkeypatch.setenv("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_USER_ID", "7")
    monkeypatch.setenv("REMOTE_AGENTS_OWNER_CHAT_ID", "11")
    home = tmp_path / "home"
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories()
    (home / "dev").mkdir()
    config = AppConfig(home / "dev", home / "registry.yaml", paths.database_path, 40, 10, 30)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    ui = open_ui_database(ui_database_path(paths.database_path))
    composition = _private_boundary(config, connection, paths, load_secrets(), ui_connection=ui)
    return composition, paths, connection, ui


async def _draw_the_sessions_page(boundary) -> None:
    """Render the page the loop used to run on, minting a fresh keyboard as it goes."""
    from fake_telegram import FakeChat

    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)


@pytest.mark.asyncio
async def test_watcher_ignores_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render writes to the UI store, and the watched file does not move.

    The measurement, not the intention: a full sessions render mints a callback token per button
    — the write that used to land in the watched file and republish itself.
    """
    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    watch = StoreWatch(watched_paths(paths.database_path))
    try:
        await watch.poll_once()  # establish the baseline reading

        def fingerprints() -> list[int | None]:
            return [
                path.stat().st_mtime_ns if path.exists() else None
                for path in watched_paths(paths.database_path)
            ]

        before = fingerprints()
        await _draw_the_sessions_page(composition.boundary)
        minted = ui.execute("SELECT COUNT(*) FROM callback_states").fetchone()[0]
        after = fingerprints()

        assert minted, "the render minted no tokens, so it did not exercise the write at issue"
        assert before == after, "the render moved the watched file"
        assert await watch.poll_once() is False, (
            "a render published a store change; the redraw loop is back"
        )
    finally:
        watch.stop()
        connection.close()
        ui.close()


@pytest.mark.asyncio
async def test_watcher_reports_session_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the other half, because a watcher that never fires would pass the test above."""
    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    watch = StoreWatch(watched_paths(paths.database_path))
    try:
        await watch.poll_once()

        connection.execute(
            "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, "
            "state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "p1", "claude", "Demo", "running", "2026-09-14T00:00:00+00:00"),
        )
        connection.commit()

        assert await watch.poll_once() is True, (
            "a real session write went unnoticed; the surfaces would never learn of it"
        )
    finally:
        watch.stop()
        connection.close()
        ui.close()


@pytest.mark.asyncio
async def test_composition_render_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observed at the subscriber, through the composition's own wiring.

    The two tests above read the file and ask the watcher directly. This one asks what the
    surfaces are actually *told*, which is the fact the loop turned on: a listener subscribed the
    way `_redraw_sessions_on_store_changes` subscribes, and a render that must not reach it.
    """
    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    watch = StoreWatch(watched_paths(paths.database_path))
    heard: list[object] = []
    # `subscribe` starts the real background poll at its one-second interval, and this test also
    # calls `poll_once` by hand. That is race-free only because `poll_once` contains no `await`,
    # so it cannot yield to the background task mid-assertion — an implementation coincidence,
    # not a documented invariant. If `poll_once` ever gains an await (a threaded `os.stat`, say),
    # this test starts flaking and the reason will not be obvious. Noted so it is.
    unsubscribe = watch.subscribe(heard.append)
    try:
        await watch.poll_once()
        heard.clear()

        await _draw_the_sessions_page(composition.boundary)
        await watch.poll_once()

        assert not heard, f"a render told the surfaces the store changed: {heard}"

        # And the subscriber is live, so "heard nothing" is not "was never listening".
        connection.execute(
            "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, "
            "state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("s2", "p1", "claude", "Demo", "running", "2026-09-14T00:00:01+00:00"),
        )
        connection.commit()
        await watch.poll_once()
        assert heard, "the subscriber heard nothing at all, so the silence above proved nothing"
    finally:
        unsubscribe()
        watch.stop()
        connection.close()
        ui.close()


def test_the_watched_file_is_not_the_one_the_surface_writes(tmp_path: Path) -> None:
    """The structural half, stated once: these are two different files.

    Cheap, and it fails for a reason the behavioural tests above would report as something else
    if the two names ever converged.
    """
    domain = tmp_path / "sessions.sqlite3"
    assert ui_database_path(domain) not in set(watched_paths(domain))


@pytest.mark.asyncio
async def test_the_redraw_entry_point_itself_leaves_the_watched_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident path, driven through the method a `StoreChanged` actually reaches.

    **The other tests in this file do not reach it.** They drive `sessions_command`, which shares
    `_sessions_reply` with `redraw_sessions_if_open` but skips its three refusal guards — so the
    suite never put the system in the state that produced the flood: the sessions page genuinely
    open, a real store write arriving, and the redraw minting a fresh keyboard and sending it.
    A review caught that the plan's central evidence had this hole, and that only the manual
    five-minute drive covered it.

    So: open the page for real, change what it shows (the content guard added by the flood hotfix
    refuses an identical redraw, and refusing would prove nothing), then call the entry point and
    watch the watched file.
    """
    from fake_telegram import FakeChat

    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    boundary = composition.boundary
    watch = StoreWatch(watched_paths(paths.database_path))
    try:
        chat = FakeChat()
        await boundary.sessions_command(chat.message_update("/sessions"), None)
        assert boundary.view.showing("sessions"), "the page is not open; the guards would refuse"

        # A real session write — the thing the watcher is supposed to report — so the redraw has
        # something new to draw and will mint rather than refuse. Written through the store
        # rather than as raw SQL, because the page parses `display_identity` and a hand-spelled
        # one is a second opinion about an encoding this test has no business knowing.
        from datetime import UTC, datetime
        from uuid import UUID

        from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
        from remote_agents.domain.models import (
            ProfileId,
            ProjectId,
            SessionDisplayIdentity,
            SessionId,
            SessionRecord,
            SessionState,
        )

        await SQLiteSessionStore(connection).save(
            SessionRecord(
                SessionId(UUID(int=9)),
                ProjectId("a" * 24),
                ProfileId("claude"),
                SessionDisplayIdentity("Demo", "Claude", "regular", 1),
                SessionState.RUNNING,
                datetime(2026, 9, 14, tzinfo=UTC),
            )
        )
        await watch.poll_once()  # consume the change that write legitimately published

        boundary._redraw_allowed_at = 0.0  # noqa: SLF001 -- the floor is not what is under test
        minted_before = ui.execute("SELECT COUNT(*) FROM callback_states").fetchone()[0]
        domain_before = paths.database_path.stat().st_mtime_ns

        drew = await boundary.redraw_sessions_if_open(chat.bot)

        minted_after = ui.execute("SELECT COUNT(*) FROM callback_states").fetchone()[0]
        domain_after = paths.database_path.stat().st_mtime_ns

        assert drew is True, "the redraw refused, so it never minted and this proved nothing"
        assert minted_after > minted_before, "no keyboard was minted; the write at issue is absent"
        assert domain_after == domain_before, (
            "the redraw moved the watched file — it would republish the change that scheduled it"
        )
        assert await watch.poll_once() is False, (
            "the redraw published a store change; this is the flood loop, reconstituted"
        )
    finally:
        watch.stop()
        connection.close()
        ui.close()
