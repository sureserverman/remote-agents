"""Processes that start together on a fresh home must not trip over each other.

The console starts four pane processes at once, and each runs the same first-start path: make
the private directories, then open the store, which backs it up and migrates it. Nothing
serialized them. A loser crashed with `FileExistsError` from a `mkdir` another process had just
made, with "disk I/O error" from a backup another was writing, or with "table ... already
exists" from a migration another had just applied, and tmux closed its pane. Measured
2026-11-14 in `tests/e2e/test_console_panes.py`: about one fresh console in nine lost its
sessions pane that way, at v0.69.0 as well as after it.

Threads, not processes: they hit the same races in well under a second, on Linux and macOS, and
`flock` locks belong to each open of the file, so two threads contend exactly as two processes
do.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, current_version
from remote_agents.ports.private_directory import open_private_directory
from remote_agents.production import ProductionPaths

_STARTERS = 8
_TRIALS = 20


def _together(start: Callable[[], object]) -> list[str]:
    """Run `start` on every starter at once; answer each one's failure, if any."""
    barrier = threading.Barrier(_STARTERS)
    failures: list[str] = []

    def starter() -> None:
        barrier.wait()
        try:
            start()
        except Exception as error:  # noqa: BLE001 -- every failure is the finding
            failures.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=starter) for _ in range(_STARTERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return failures


def test_starters_opening_one_fresh_store_together_all_get_it_migrated(tmp_path: Path) -> None:
    failures: list[str] = []
    for trial in range(_TRIALS):
        directory = tmp_path / f"state-{trial}"
        directory.mkdir(mode=0o700)
        store = directory / "sessions.sqlite3"

        def start(store: Path = store) -> None:
            connection = open_database(store)
            try:
                assert current_version(connection) == MIGRATIONS[-1][0]
            finally:
                connection.close()

        failures += _together(start)
    assert failures == []


def test_starters_making_one_production_home_together_all_succeed(tmp_path: Path) -> None:
    failures: list[str] = []
    for trial in range(_TRIALS):
        home = tmp_path / f"home-{trial}"
        home.mkdir(mode=0o700)
        paths = ProductionPaths.for_home(home)
        failures += _together(
            lambda paths=paths: paths.ensure_directories(include_unit_directory=False)
        )
    assert failures == []


def test_starters_making_one_private_directory_together_all_get_it(tmp_path: Path) -> None:
    failures: list[str] = []
    for trial in range(_TRIALS):
        target = tmp_path / f"root-{trial}" / "state" / "activity"

        def start(target: Path = target) -> None:
            if open_private_directory(target) != target:
                raise AssertionError("refused a directory another starter had just made")

        failures += _together(start)
    assert failures == []
