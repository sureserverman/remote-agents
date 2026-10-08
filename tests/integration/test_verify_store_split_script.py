"""`scripts/verify-store-split.py` runs in the suite, so it cannot rot as the stores move (BL-091).

It is the one instrument that sweeps every moved table in both directions, and it was hand-run
at the store-split gates and by nothing since. These tests drive it as a script, the way a gate
does, against a store built here: the passing claims, and one failing case per moved table, so
a verifier that stopped looking at a table would be caught here rather than trusted.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_store_split_migration import _a_store_with_rows

from remote_agents.adapters.sqlite.database import open_database, ui_database_path
from remote_agents.adapters.sqlite.migrations import MOVED_TABLES
from remote_agents.adapters.sqlite.store_split import split_stores

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-store-split.py"


def _verify(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def split(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A pre-split store with a row in every moved table, its copy, and the split it became."""
    domain = tmp_path / "sessions.sqlite3"
    _a_store_with_rows(domain)
    before = tmp_path / "before.sqlite3"
    shutil.copy(domain, before)
    split_stores(domain)
    return before, domain, ui_database_path(domain)


def test_every_moved_row_is_accounted_for(split: tuple[Path, Path, Path]) -> None:
    before, domain, ui = split
    result = _verify("--counts", "--before", before, "--after", domain, "--ui", ui)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_second_split_changes_no_count(split: tuple[Path, Path, Path]) -> None:
    _before, domain, ui = split
    result = _verify("--idempotent", "--domain", domain, "--ui", ui)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_stores_are_disjoint_once_the_drop_has_run(split: tuple[Path, Path, Path]) -> None:
    _before, domain, ui = split
    open_database(domain).close()  # applies migration 14, which drops the moved tables
    result = _verify("--disjoint", "--domain", domain, "--ui", ui)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("table", MOVED_TABLES)
def test_a_row_missing_from_the_ui_store_fails_and_is_named(
    split: tuple[Path, Path, Path], table: str
) -> None:
    before, domain, ui = split
    connection = sqlite3.connect(ui)
    try:
        connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
    finally:
        connection.close()

    result = _verify("--counts", "--before", before, "--after", domain, "--ui", ui)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{table}:" in result.stdout


@pytest.mark.parametrize("table", MOVED_TABLES)
def test_a_moved_table_back_in_the_domain_store_fails_and_is_named(
    split: tuple[Path, Path, Path], table: str
) -> None:
    _before, domain, ui = split
    open_database(domain).close()
    connection = sqlite3.connect(domain)
    try:
        connection.execute(f'CREATE TABLE "{table}" (resurrected INTEGER)')
        connection.commit()
    finally:
        connection.close()

    result = _verify("--disjoint", "--domain", domain, "--ui", ui)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{table} still in the domain store" in result.stdout
