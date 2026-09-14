"""Which database each store the composition builds actually writes to.

**The whole split turns on this file.** `StoreWatch` fingerprints the domain store, so a surface
store still holding the domain connection keeps publishing the change that retriggers its own
redraw — the loop that flood-banned the bot. Moving the tables without moving the readers would
leave every symptom in place and every test green.

Swept over the stores the composition actually builds rather than named one at a time: a fifth
surface store added later and wired to the wrong connection is exactly the regression this must
catch, and a list of four cannot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import (
    open_database,
    open_ui_database,
    ui_database_path,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, UI_TABLES


def _main_file(connection: sqlite3.Connection) -> Path:
    """The file a connection's `main` schema is backed by."""
    for _, name, path in connection.execute("PRAGMA database_list"):
        if name == "main":
            return Path(path)
    raise AssertionError("connection has no main database")


def _stores_with_connections(boundary: object, *, depth: int = 2) -> dict[str, object]:
    """Every store reachable from the boundary that holds a SQLite connection of its own.

    **Reaches through wrappers, and the first version did not.** It looked only at direct
    attributes, so it found `callbacks`, `anchors` and `standing` — and silently missed
    `trust_store`, which is not a boundary field at all but lives inside the trust notifier at
    `trust_notifier._store`. Three of four, in the test whose whole stated purpose is catching a
    store wired to the wrong database. Rewiring `trust_store` to the domain connection would have
    passed this file completely.

    The count assertion at the call site is the other half: reaching further is a fix for the
    store we know about, while a count that must equal `UI_TABLES` is what makes the *next*
    invisible store fail loudly instead of by omission.
    """
    found: dict[str, object] = {}

    def walk(obj: object, path: str, left: int) -> None:
        if left < 0:
            return
        for name in dir(obj):
            if name.startswith("__"):
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if isinstance(getattr(value, "_connection", None), sqlite3.Connection):
                found.setdefault(f"{path}{name}", value)
            elif left and not isinstance(value, str | bytes | int | float | bool | type(None)):
                if type(value).__module__.startswith("remote_agents"):
                    walk(value, f"{path}{name}.", left - 1)

    walk(boundary, "", depth)
    return found


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


def test_every_surface_store_writes_to_the_ui_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swept, not listed. A store holding the domain connection is the loop, still armed."""
    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        stores = _stores_with_connections(composition.boundary)
        assert stores, "no store on the boundary holds a connection; this swept nothing"

        domain_file = paths.database_path.resolve()
        ui_file = ui_database_path(paths.database_path).resolve()

        # Deduplicated by identity, because one store is reachable by several routes (the live
        # view and the notifier both hold the callback store) and a path count would say 17.
        # What the claim is about is distinct stores.
        by_file: dict[Path, dict[int, str]] = {}
        stray = {}
        for name, store in stores.items():
            actual = _main_file(store._connection).resolve()  # noqa: SLF001
            if actual not in {domain_file, ui_file}:
                stray[name] = actual.name
            by_file.setdefault(actual, {}).setdefault(id(store), name)
        assert not stray, f"stores holding a connection to neither store: {stray}"

        # **One distinct store per moved table, and the equality is the whole guard.** Too few
        # means a surface store is wired to the domain connection — the loop, still armed. Too
        # many means something domain-owned followed the tables across. Either way this fails,
        # which a per-store expectation derived from the store's own connection could not: the
        # first version of this test did exactly that, and rewiring `anchors` back to the domain
        # connection agreed with itself and passed.
        on_ui = by_file.get(ui_file, {})
        assert len(on_ui) == len(UI_TABLES), (
            f"{len(on_ui)} distinct stores on the UI database for {len(UI_TABLES)} moved "
            f"tables: {sorted(on_ui.values())}"
        )

        # And the domain side is not empty, so "everything moved" cannot pass the line above.
        assert by_file.get(domain_file), "no store left on the domain database at all"
    finally:
        connection.close()
        ui.close()


def test_the_surface_tables_are_gone_from_the_watched_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 14's half of the same change: the readers move and the tables go together."""
    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        present = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
        ui.close()

    assert not present & set(UI_TABLES), f"still in the watched store: {present & set(UI_TABLES)}"


def test_the_domain_store_still_owns_what_crosses_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`idempotency_claims` must not follow the surface tables out.

    `session_store.py` writes it and `docs/architecture.md` guarantees duplicate-command
    protection is durable across processes. In the UI store the bot's claims would sit in a file
    the console panes never open, and every writer but one would stop seeing them.
    """
    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        in_domain = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='idempotency_claims'"
        ).fetchone()
        in_ui = ui.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='idempotency_claims'"
        ).fetchone()
    finally:
        connection.close()
        ui.close()

    assert in_domain and not in_ui


def test_the_stores_are_opened_split_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bootstrap's order, not the order a test happens to call them in.

    `test_the_rows_survive_a_domain_open_that_applies_the_drop` proves the ordering matters; it
    does not prove production honours it. Reversing the two calls in bootstrap left every test
    green, which is how this gap was found.
    """
    from remote_agents import bootstrap
    from remote_agents.production import ProductionPaths

    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "split_stores", lambda *_a, **_k: calls.append("split"))
    monkeypatch.setattr(bootstrap, "open_ui_database", lambda *_a, **_k: calls.append("ui"))

    class _Paths(ProductionPaths):
        def open_database(self, *_args, **_kwargs):
            calls.append("domain")
            return object()

    home = tmp_path / "home"
    paths = _Paths.for_home(home)
    paths.ensure_directories()
    bootstrap._open_both_stores(paths, False)

    assert calls[0] == "split", f"the domain store was opened before the split: {calls}"
    assert calls.index("split") < calls.index("domain")


def test_no_domain_open_bypasses_the_split() -> None:
    """An absence, swept over the whole of `src/` by parsing it — not grepped in one file.

    The hazard is a *missing* call, which adds nothing for a reviewer to notice: a command that
    opens the domain store the obvious way applies migration 14 to a store nothing has split,
    drops the surface rows, and — because `split_stores` decides there is work by finding those
    tables — leaves every later split a silent no-op. Forever. That is what happened here: the
    split lived in `_open_both_stores`, used only by `serve`, while `tui`, `pane` and `--history`
    opened directly.

    **This was a grep over one file, and a review showed what that missed.** It read only
    `bootstrap.py` and matched the literal `migrations=MIGRATIONS`, so it would not have caught a
    bypass in another module, a positional call, an aliased list — or, worst and most natural, a
    bare `open_database(path)`, since that parameter *defaults* to the domain list. So it parses
    instead, over every module, and asks the question that actually matters: does anything call
    `open_database` outside the one function allowed to?

    Dropping that default was the other option offered, and it is a fine one; it is not taken
    because 159 call sites rely on it and every single one is a test legitimately opening a
    domain store. None are in `src/`. The churn buys a `TypeError` where this buys a named
    failure, so the trade was judged rather than defaulted to.
    """
    import ast

    root = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
    allowed = "_open_domain_store"
    offenders = []
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text())
        # Whatever this module calls it. `from ... import open_database as _od` defeated the
        # first version of this sweep, which matched the name literally — an alias was one of
        # the three miss-modes the review named, and the sweep had only fixed two of them.
        names = {"open_database"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names |= {
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "open_database"
                }
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    enclosing[id(child)] = node.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name not in names:
                continue
            # `open_ui_database` passes UI_MIGRATIONS and is a different store entirely.
            # Qualified by module, not by bare name. A second function called
            # `_open_domain_store` anywhere under src/ would otherwise exempt itself — the same
            # coincidental-name gap this sweep replaced a grep to avoid.
            # Path-relative, not just the filename: two files named `database.py` under
            # `src/` would collide the same way the original bare-name bypass did, one level up.
            here = (module.relative_to(root).as_posix(), enclosing.get(id(node)))
            exempt = {
                ("bootstrap.py", allowed),
                ("adapters/sqlite/database.py", "open_ui_database"),
            }
            if here in exempt:
                continue
            where = enclosing.get(id(node))
            offenders.append(f"{module.relative_to(root)}:{node.lineno} in {where}")

    assert not offenders, (
        "a domain-store open outside _open_domain_store: it would apply migration 14 to a store "
        f"nothing has split, and silently disable every later split. Found: {offenders}"
    )



def test_the_chokepoint_carries_the_rows_out_before_the_drop(tmp_path: Path) -> None:
    """The behaviour every entry point inherits by going through one function.

    `tui`, `pane` and `--history` are covered by this together with the sweep above: the sweep
    says they route through here, and this says what happens when they do.
    """
    from remote_agents.adapters.sqlite.store_split import split_stores
    from remote_agents.bootstrap import _open_domain_store
    from remote_agents.production import ProductionPaths

    home = tmp_path / "home"
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories()

    # A store as a real host has it before the upgrade: at 13, with surface rows in it.
    pre_split = [entry for entry in MIGRATIONS if entry[0] < 14]
    seeded = open_database(paths.database_path, migrations=pre_split)
    try:
        seeded.execute(
            "INSERT INTO callback_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("c1_tok", "session.detail", "e1", 7, 11, 100, 0, 0, "2026-09-14T00:00:00+00:00"),
        )
        seeded.commit()
    finally:
        seeded.close()
    assert split_stores is not None  # the chokepoint's collaborator, imported for clarity

    _open_domain_store(paths).close()

    ui = sqlite3.connect(ui_database_path(paths.database_path))
    try:
        carried = ui.execute("SELECT COUNT(*) FROM callback_states").fetchone()[0]
    finally:
        ui.close()
    assert carried == 1, "the drop ran without the rows being carried out first"


def test_a_failing_ui_open_does_not_leak_the_domain_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The domain store is already open when the second open can fail.

    A corrupt `ui.sqlite3`, a full disk, a bad UI migration — any of them raises after the first
    connection exists, and before this fix nothing closed it. `_serve` carries a comment worrying
    about precisely this shape for one connection; adding a second open put it back.
    """
    from remote_agents import bootstrap
    from remote_agents.production import ProductionPaths

    opened: list[sqlite3.Connection] = []

    class _Paths(ProductionPaths):
        def open_database(self, *_args, **_kwargs):
            connection = sqlite3.connect(":memory:")
            opened.append(connection)
            return connection

    monkeypatch.setattr(bootstrap, "split_stores", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bootstrap, "open_ui_database", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no disk"))
    )

    home = tmp_path / "home"
    paths = _Paths.for_home(home)
    paths.ensure_directories()

    with pytest.raises(OSError):
        bootstrap._open_both_stores(paths, False)

    assert opened, "the domain store was never opened; this proved nothing"
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
