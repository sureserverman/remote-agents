"""What the change watcher is allowed to look at.

This is the point of the split. `StoreWatch` publishes a change when the bytes it fingerprints
move, and every surface re-reads on that signal. While the surface's own writes landed in the
watched file, the bot minting a keyboard *was* a session changing as far as the watcher could
tell — so an open sessions page republished its own change and redrew, about thirty times a
minute, until Telegram flood-banned the bot for six hours.

Moving the tables (Task 2.1) stops the bot writing there. Narrowing `watched_paths` is what makes
the signal *mean* something: everything left in that file is written by another process.
"""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.sqlite.database import ui_database_path, watched_paths


def test_the_ui_store_is_not_watched() -> None:
    """The one assertion this task exists for."""
    domain = Path("/state/sessions.sqlite3")
    watched = set(watched_paths(domain))

    assert ui_database_path(domain) not in watched
    assert Path(f"{ui_database_path(domain)}-wal") not in watched


def test_the_domain_store_is_still_watched() -> None:
    """A watcher that looks at nothing would pass the test above perfectly."""
    domain = Path("/state/sessions.sqlite3")
    watched = set(watched_paths(domain))

    assert domain in watched
    assert watched, "watched_paths returned nothing; the surfaces would never learn of a change"


def test_nothing_outside_the_domain_store_is_watched() -> None:
    """Swept, rather than asserting the absence of the one file we happen to know about.

    A future sibling — a cache, an index, a second surface store — added to this list would
    reintroduce the defect for a different file, and an assertion naming `ui.sqlite3` could not
    fail on it.
    """
    domain = Path("/state/sessions.sqlite3")
    allowed = {domain, Path(f"{domain}-wal")}

    assert set(watched_paths(domain)) <= allowed, (
        f"watching something other than the domain store: {set(watched_paths(domain)) - allowed}"
    )
