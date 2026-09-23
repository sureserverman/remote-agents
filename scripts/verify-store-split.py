#!/usr/bin/env python3
"""Prove the store split moved every row and left the two files disjoint.

The stage gates will call this once the split is wired in — nothing invokes it at HEAD.
It exists because the claims are about a **set** — every moved table, in both
directions — and a check that names one table cannot fail on the other three. `MOVED_TABLES` is
imported rather than restated here so the set has one definition (DEC-011); a table added to
the migration and forgotten here would otherwise pass a verifier that never looked for it.

    --counts   --before A --after B --ui C   every moved table: rows(A) <= rows(C), the UI
                                             store alone; B is reported, never summed in
    --idempotent --domain D --ui U           a second split changes no count in either file
    --disjoint --domain D --ui U             neither file carries the other's tables
                                             (Stage 2 onward: during Stage 1 the moved tables are
                                             still in the domain store by design, so this FAILS
                                             correctly and is not run)

Exit 0 when the claim holds, 1 when it does not, and the failure names the table.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remote_agents.adapters.sqlite.migrations import MOVED_TABLES, UI_TABLES  # noqa: E402


def _domain_tables(domain: Path) -> set[str]:
    """Everything the domain store holds that is not the moved set.

    Derived, not listed. A hand-maintained enumeration here would be a second opinion about
    which tables are domain-owned (DEC-011), and the half that drifted would be this one:
    a new domain table added upstream would simply be absent from it, so a leak into the UI
    store would pass the one script whose job is catching exactly that.
    """
    # `schema_version` is excluded because each file legitimately carries its own: the two
    # stores version independently, which is the point. Deriving the set without this exclusion
    # reported it as "leaked into the UI store" on the first run.
    return _tables(domain) - set(MOVED_TABLES) - {"schema_version"}


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()


def _counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    present = _tables(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
            if table in present
        }
    finally:
        connection.close()


def _report(failures: list[str], claim: str) -> int:
    if failures:
        print(f"FAIL {claim}")
        for line in failures:
            print(f"  {line}")
        return 1
    print(f"OK {claim} — swept {len(MOVED_TABLES)} table(s): {', '.join(sorted(MOVED_TABLES))}")
    return 0


def _counts_claim(before: Path, after: Path, ui: Path) -> int:
    """Every row the domain store had is now in the **UI** store.

    **Against the UI store specifically, not the two summed.** The sum was the first version
    and it could not fail: Stage 1 is additive, so the domain store still holds every row, and
    `after + ui` cleared the bar however empty `ui` was. Deleting five rows from the UI store
    and watching the check still report OK is how that was found — a verifier that passes while
    measuring nothing is worse than no verifier, because the gate it guards reads green.

    `after` stays in the signature because Stage 2's drop makes it the other half of the same
    claim, and it is reported when a row is in neither file.
    """
    was = _counts(before, MOVED_TABLES)
    failures = []
    for table in MOVED_TABLES:
        expected = was.get(table, 0)
        landed = _counts(ui, (table,)).get(table, 0)
        if landed < expected:
            left = _counts(after, (table,)).get(table, 0)
            failures.append(
                f"{table}: {expected} before, {landed} in the UI store "
                f"({left} still in the domain store)"
            )
    if not was:
        failures.append(
            f"{before} carried none of the moved tables, so this proved nothing about moving rows"
        )
    return _report(failures, "every moved row is accounted for")


def _idempotent_claim(domain: Path, ui: Path) -> int:
    """Running the split again changes nothing — it runs on every process start."""
    from remote_agents.adapters.sqlite.store_split import split_stores

    before = _counts(ui, MOVED_TABLES)
    split_stores(domain)
    after = _counts(ui, MOVED_TABLES)
    failures = [
        f"{table}: {before.get(table)} -> {after.get(table)}"
        for table in MOVED_TABLES
        if before.get(table) != after.get(table)
    ]
    return _report(failures, "a second split changes no count")


def _disjoint_claim(domain: Path, ui: Path) -> int:
    """Neither file carries the other's tables — the split is only worth anything if it is clean."""
    in_domain = _tables(domain)
    in_ui = _tables(ui)
    # Every UI table, not only the moved ones: a table born in the UI store must not appear in
    # the domain store either.
    failures = [f"{t} still in the domain store" for t in sorted(set(UI_TABLES) & in_domain)]
    # `handoff_intents` is unioned in explicitly. It is dropped by migration 14 rather than
    # moved, so it is in neither store — which means `_domain_tables(domain)` cannot contain it
    # and a copy that resurrected it would have passed silently. The comment above claimed this
    # was covered before it was.
    never_in_ui = _domain_tables(domain) | {"handoff_intents"}
    failures += [f"{t} leaked into the UI store" for t in sorted(never_in_ui & in_ui)]
    return _report(failures, "the two stores are disjoint in both directions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", action="store_true")
    parser.add_argument("--idempotent", action="store_true")
    parser.add_argument("--disjoint", action="store_true")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--domain", type=Path)
    parser.add_argument("--ui", type=Path)
    args = parser.parse_args()

    if args.counts:
        if not (args.before and args.after and args.ui):
            parser.error("--counts needs --before, --after and --ui")
        return _counts_claim(args.before, args.after, args.ui)
    if args.idempotent:
        if not (args.domain and args.ui):
            parser.error("--idempotent needs --domain and --ui")
        return _idempotent_claim(args.domain, args.ui)
    if args.disjoint:
        if not (args.domain and args.ui):
            parser.error("--disjoint needs --domain and --ui")
        return _disjoint_claim(args.domain, args.ui)
    parser.error("pick one of --counts, --idempotent, --disjoint")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
