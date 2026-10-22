"""The rollover pass is the bot's service alone (DEC-115, DEC-030): `serve` builds and runs it,
and the local surface's composition names neither the pass nor its loop, so two processes can
never both launch, type or stop for one rollover."""

from __future__ import annotations

import ast
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.agents.reported_roots import FileReportedRoots
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.application.rollover import RolloverPass
from remote_agents.application.rollover_book import RolloverBook
from remote_agents.composition.service import reported_roots_reader
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "remote_agents"


def test_the_local_surface_composes_no_rollover_pass() -> None:
    tree = ast.parse((SRC / "composition" / "tui.py").read_text(encoding="utf-8"))
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not (
        {"RolloverPass", "build_rollover_pass", "_roll_over_periodically"} & (names | imported)
    )


def test_the_bot_composition_wires_the_pass_and_both_surfaces_get_the_book(
    tmp_path, monkeypatch
) -> None:
    from test_composition_store_wiring import _composition

    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        assert isinstance(composition.rollover_pass, RolloverPass)
        # The roots `remote-agents session-id` recorded are read by the pass (DEC-131).
        assert composition.rollover_pass._reported_roots is not None
        assert isinstance(composition.boundary.backend.rollovers, RolloverBook)
        # The pass types what the verticals declare, and the surfaces offer Rollover now on
        # exactly the profiles it acts for: one map, read from the composition's descriptors.
        assert composition.rollover_pass.adopt_templates == adopt_templates()
        assert set(composition.rollover_pass.adopt_templates) == {
            ProfileId("claude"),
            ProfileId("codex"),
        }
        assert composition.boundary.backend.rollovers.rollable == frozenset(
            composition.rollover_pass.adopt_templates
        )
        # Failures are told through the bot, and a held notice is retried by the loop.
        assert composition.rollover_notifier is not None
        assert composition.rollover_notifier is composition.boundary.rollover_notifier
    finally:
        connection.close()
        ui.close()


def test_only_the_bot_composition_builds_or_runs_the_pass_anywhere_in_the_package() -> None:
    """Swept across every module, not only `composition/`: a pass built from `bootstrap`, an
    adapter or the TUI by any route -- the builder, the field, the loop -- would be a second
    process acting on panes the first one owns (DEC-030)."""
    allowed = {
        "build_rollover_pass": {"composition/service.py", "composition/telegram.py"},
        "rollover_pass=": {"composition/telegram.py"},
        "_roll_over_periodically": {"composition/service.py"},
        "RolloverPass(": {"composition/service.py"},
    }
    found: dict[str, set[str]] = {name: set() for name in allowed}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in allowed:
            if name in text:
                found[name].add(str(path.relative_to(SRC)))
    found["RolloverPass("].discard("application/rollover.py")  # its own definition
    assert found["build_rollover_pass"], "the sweep found nothing to mean anything"
    for name, modules in found.items():
        assert modules <= allowed[name], (name, modules - allowed[name])


# -- the roots sessions' writers reported (DEC-131) ------------------------------------------


def _top_level(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--template=", str(path)], check=True, timeout=60)
    return path.resolve()


def _listed(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("remote-agents"),
        ProfileId("claude"),
        SessionDisplayIdentity("remote-agents", "claude", "regular", 1, None),
        state,
        datetime(2026, 10, 22, tzinfo=UTC),
    )


async def test_the_pass_reads_each_listed_sessions_accepted_report_and_forgets_the_rest(
    tmp_path,
) -> None:
    store = FileReportedRoots(tmp_path / "activity", accept=acceptable_root)
    running, ended, unlisted, stale = (SessionId.new() for _ in range(4))
    there = _top_level(tmp_path / "there")
    gone = _top_level(tmp_path / "gone")
    for session in (running, ended, unlisted):
        assert store.record(str(session), there)
    assert store.record(str(stale), gone)
    # It stopped being a top level after it was reported: it reads as no report at all.
    (gone / ".git").rename(gone / "was-git")

    async def listed():
        return (
            _listed(running, SessionState.RUNNING),
            _listed(ended, SessionState.ENDED),
            _listed(stale, SessionState.RUNNING),
        )

    async def held():
        return ()

    roots = await reported_roots_reader(store, listed, held)()

    assert roots == {running: there}
    assert set(store.sessions()) == {str(running), str(stale)}


async def test_an_ended_session_an_open_rollover_names_keeps_its_report(tmp_path) -> None:
    """A stopped predecessor is ENDED while its rollover is still open: the successor's
    `accepted` and the clean-up are read under its root, so the report stays until no open
    rollover names it."""
    store = FileReportedRoots(tmp_path / "activity", accept=acceptable_root)
    predecessor, successor = SessionId.new(), SessionId.new()
    there = _top_level(tmp_path / "there")
    assert store.record(str(predecessor), there)

    async def listed():
        return (
            _listed(predecessor, SessionState.ENDED),
            _listed(successor, SessionState.RUNNING),
        )

    async def held():
        return (predecessor, successor)

    roots = await reported_roots_reader(store, listed, held)()

    assert roots == {predecessor: there}
    assert set(store.sessions()) == {str(predecessor)}
