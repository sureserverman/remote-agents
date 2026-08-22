"""Opening a session list is one shared read, and only one surface syncs a console after it.

Both frontends did the same two things in the same order on list open — run the readiness
pass, then read the listable records — and nothing held them together. That pairing is the
duplicate: `only_listed` was already shared (Stage 2) and `refresh_readiness` was always one
method, but *that these two go together, on this screen and not on the ones beside it* was
written twice and asserted nowhere.

**It matters which reads are paired and which are not.** Neither surface refreshes on the
paths that re-read a single session — the bot's `_record`, the local surface's
`current_record` — and both say why in their own words: the pass rescans every record and
runs a tmux capture per FAILED session, so repeating it per navigation would make opening a
detail and copying its attach command cost three full passes. A guard that only checked "the
refresh happens somewhere" would be satisfied by a surface that had started doing it
everywhere.

So the sweeps below check the shape rather than the outcome: no frontend calls
`refresh_readiness` at all any more, because pairing it with a read is the use case's job and
the surface's only remaining choice is whether to open a list. That is the form a re-fork
would actually take — not a function someone names, but two adjacent `await`s someone writes
again — which is the lesson Stage 2 paid for and Task 3.3 repeated.

**The console half is ARCH-B3, and it is an asymmetry worth pinning rather than tidying.**
The local surface syncs the console with what it just read; the bot never does, because it
does not host a console. Both processes *do* wire `hide_in_console`, from two different
composers — so "the bot has nothing to do with the console" is the wrong summary and the
plan's ARCH-B3 was corrected once already for saying it. The precise claim, and the one below,
is narrower: only the local surface arranges a console around a list it has just drawn.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents"
_FRONTENDS = {"telegram": _SRC / "adapters" / "telegram", "tui": _SRC / "adapters" / "tui"}


def _sources(tree_root: pathlib.Path) -> list[pathlib.Path]:
    assert tree_root.is_dir(), f"{tree_root} must exist or this sweep passes over nothing"
    return sorted(tree_root.rglob("*.py"))


def _calls_named(tree_root: pathlib.Path, name: str) -> list[str]:
    """Every call to `name`, however it is reached — a bare name or an attribute."""
    found = []
    for path in _sources(tree_root):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if called == name:
                found.append(f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}")
    return found


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
def test_no_frontend_runs_the_readiness_pass_itself(frontend: str) -> None:
    """The pass belongs to the read it precedes, so a surface never orders the two.

    This is the whole duplicate. Both surfaces wrote `await …refresh_readiness()` and then a
    list read, and a surface that keeps its own copy of that order is one refactor away from
    running the pass on a screen that should not pay for it — or from dropping it on the one
    screen that must.
    """
    offenders = _calls_named(_FRONTENDS[frontend], "refresh_readiness")

    assert offenders == [], f"{frontend} pairs the readiness pass with a read itself: {offenders}"


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
def test_each_frontend_opens_its_list_through_the_one_shared_read(frontend: str) -> None:
    """Exactly one call site per surface: the list, and nothing else.

    Two would mean a second screen had quietly started paying for the readiness pass; none
    would mean the surface had stopped refreshing on list open, which is the regression the
    bot's own comment about the cost is describing the other side of.
    """
    calls = _calls_named(_FRONTENDS[frontend], "listed_sessions")

    assert len(calls) == 1, f"{frontend} list-open read sites: {calls}"


def test_only_the_local_surface_syncs_a_console_after_reading() -> None:
    """ARCH-B3, stated as the narrow claim rather than the tidy one."""
    assert _calls_named(_FRONTENDS["telegram"], "console_sync") == []
    assert _calls_named(_FRONTENDS["tui"], "console_sync") != []
