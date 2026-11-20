"""A rollover types its template only once the successor's thread is bound.

The template's adoption asks `session-id`, which refuses an unbound thread, so typing first would
only fail later, after a predecessor had waited out the adoption's patience. A successor that cannot
be bound fails the rollover by name, and its predecessor runs on.
"""

from __future__ import annotations

import pytest

from remote_agents.application.permission_apply import READY, Readiness
from remote_agents.application.rollover import BINDING_FAILED, PERMISSIONS_FAILED, RolloverPass
from remote_agents.domain.models import SessionState
from remote_agents.domain.rollover import RolloverState

from .test_rollover_pass import (  # noqa: F401
    CLAUDE,
    PROJECT,
    ROOT,
    TEMPLATE,
    TEMPLATES,
    Rig,
    _trust,
    rig,
)


def _gate(test_rig: Rig, ready) -> None:  # noqa: F811
    """Give the rig's pass `ready` as what it waits on before it types."""
    test_rig._pass = RolloverPass(
        test_rig.store,
        test_rig.envelopes,
        adopt_templates={CLAUDE: TEMPLATES[CLAUDE]},
        sessions=test_rig.list_sessions,
        handoff_root=lambda project: ROOT if project == PROJECT else None,
        enabled=test_rig.enabled,
        idle=test_rig.idle,
        launch=test_rig.launch,
        send=test_rig.send,
        ready=ready,
        graceful_stop=test_rig.graceful_stop,
        notify=test_rig.told,
        now=lambda: test_rig.clock[0],
        sleep=test_rig.sleep,
        owner_dialog=lambda session_id: test_rig.owner_dialog(session_id),
    )


def _binding(
    test_rig: Rig,  # noqa: F811
    answer: bool | Exception | Readiness,
) -> list[tuple[object, object]]:
    """Give the rig's pass a readiness that answers `answer`, and return who it was asked about.

    A bare bool is the binding alone, with nothing owed on permissions."""
    asked: list[tuple[object, object]] = []

    async def ready(session_id, profile_id) -> Readiness:
        asked.append((session_id, profile_id, list(test_rig.sends)))
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Readiness) else Readiness(bound=answer)

    _gate(test_rig, ready)
    return asked


async def test_the_template_is_typed_only_after_the_binding_is_seen(rig: Rig) -> None:  # noqa: F811
    asked = _binding(rig, True)
    rig.ready()
    await rig.run(passes=2)
    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert asked[0][:2] == (rig.successor, CLAUDE)
    assert asked[0][2] == [], "asked before anything was typed"
    assert (await rig.rollover()).state is RolloverState.ADOPTING


@pytest.mark.parametrize("answer", [False, RuntimeError("the store went away")])
async def test_a_binding_not_seen_fails_the_rollover_and_keeps_the_predecessor(
    rig: Rig,  # noqa: F811
    answer: bool | Exception,
) -> None:
    _binding(rig, answer)
    rig.ready()
    await rig.run(passes=3)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == BINDING_FAILED
    assert rig.sends == [] and rig.stops == []
    assert rig.sessions[rig.predecessor].state is SessionState.RUNNING
    assert len(rig.reports) == 1
    assert len(rig.launches) == 1


async def test_a_trust_held_successor_is_typed_only_after_trust_and_binding(
    rig: Rig,  # noqa: F811
) -> None:
    asked = _binding(rig, True)
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run(passes=3)
    assert asked == [] and rig.sends == []
    _trust(rig)
    await rig.run(passes=3)
    assert len(asked) == 1
    assert rig.sends == [(rig.successor, TEMPLATE)]


async def test_a_trusted_successor_that_cannot_bind_fails_before_adopting(
    rig: Rig,  # noqa: F811
) -> None:
    _binding(rig, False)
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    _trust(rig)
    await rig.run(passes=2)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == BINDING_FAILED
    assert rig.sends == [] and rig.stops == []


async def test_no_binding_wired_changes_nothing(rig: Rig) -> None:  # noqa: F811
    rig.ready()
    await rig.run(passes=2)
    assert rig.sends == [(rig.successor, TEMPLATE)]


async def test_a_predecessor_that_went_busy_during_the_launch_is_not_handed_over(
    rig: Rig,  # noqa: F811
) -> None:
    """With no binding at all, as on Claude and Cursor: the same two sessions on one plan."""
    from remote_agents.application.rollover import PREDECESSOR_NOT_IDLE

    real = rig.idle

    async def idle(session_id):
        return False if rig.launches else await real(session_id)

    rig.idle = idle
    rig.ready()
    await rig.run(passes=2)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == PREDECESSOR_NOT_IDLE
    assert rig.sends == []


async def test_a_predecessor_that_went_busy_during_the_wait_is_not_handed_over(
    rig: Rig,  # noqa: F811
) -> None:
    from remote_agents.application.rollover import PREDECESSOR_NOT_IDLE

    asked = _binding(rig, True)
    real = rig.idle

    async def idle(session_id):
        return False if asked else await real(session_id)

    rig._pass._idle = idle
    rig.ready()
    await rig.run(passes=2)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == PREDECESSOR_NOT_IDLE
    assert rig.sends == []


async def test_a_bound_successor_whose_permissions_fail_ends_the_rollover_and_keeps_the_predecessor(
    rig: Rig,  # noqa: F811
) -> None:
    """OD-2: the successor's pane runs on its provider's default, so the plan is not handed to
    it. The predecessor runs on, and the failure names what the daemon said."""
    asked = _binding(rig, Readiness(unapplied="timed out"))
    rig.ready()
    await rig.run(passes=3)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == PERMISSIONS_FAILED == "permissions-failed"
    assert rollover.failure_detail == "timed out"
    assert rig.sends == [] and rig.stops == []
    assert rig.sessions[rig.predecessor].state is SessionState.RUNNING
    assert len(rig.reports) == 1 and len(rig.launches) == 1
    assert len(asked) == 1, "asked once, and not again on later passes"


async def test_an_unbound_successor_owed_permissions_is_reported_as_unbound(
    rig: Rig,  # noqa: F811
) -> None:
    """Both are true; the binding is the cause, and it is the one the owner can act on."""
    _binding(rig, Readiness(bound=False, unapplied="its thread could not be bound"))
    rig.ready()
    await rig.run(passes=3)
    assert (await rig.rollover()).failure_code == BINDING_FAILED
    assert rig.sends == []


async def test_a_trusted_successor_whose_permissions_fail_fails_before_adopting(
    rig: Rig,  # noqa: F811
) -> None:
    _binding(rig, Readiness(unapplied="no Codex daemon"))
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    _trust(rig)
    await rig.run(passes=2)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == PERMISSIONS_FAILED
    assert rollover.failure_detail == "no Codex daemon"
    assert rig.sends == [] and rig.stops == []


async def test_applied_permissions_let_the_rollover_proceed_in_its_existing_order(
    rig: Rig,  # noqa: F811
) -> None:
    asked = _binding(rig, READY)
    rig.ready()
    await rig.run(passes=2)
    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert asked[0][2] == [], "asked before anything was typed"
    assert (await rig.rollover()).state is RolloverState.ADOPTING


# --- Sub-plan 5 Task 3.3: the owner's BL-144 ruling, the source's handoff folder ------------

from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from remote_agents.adapters.sqlite.database import open_database  # noqa: E402
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore  # noqa: E402
from remote_agents.application.permission_apply import (  # noqa: E402
    PermissionApplyPass,
    handoff_grant,
    handoff_grants,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId  # noqa: E402
from remote_agents.domain.profiles import PermissionPreset  # noqa: E402
from remote_agents.domain.rollover import RolloverKind  # noqa: E402
from remote_agents.ports.handoff_envelopes import HandoffDestination  # noqa: E402
from remote_agents.ports.rollover_store import OWNER_HOLD, Rollover, RolloverTarget  # noqa: E402

_AT = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
_ORCHARD = Path("/home/owner/dev/orchard")
_RIVER = Path("/home/owner/dev/river")
_SAME = object()


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two real checkouts, the source holding the handoff folder its writer made."""
    orchard, river = tmp_path / "orchard", tmp_path / "river"
    (orchard / ".claude" / "handoffs").mkdir(parents=True)
    river.mkdir()
    monkeypatch.setattr(__import__(__name__, fromlist=["_"]), "_ORCHARD", orchard)
    monkeypatch.setattr(__import__(__name__, fromlist=["_"]), "_RIVER", river)
    return orchard, river


def _target(destination: Path, version: int = 2) -> RolloverTarget:
    if version == 1:
        return RolloverTarget(
            1, ProjectId("river"), HandoffDestination(destination, "main", "/p.md", "", None),
            None, None, None, None,
        )  # fmt: skip
    return RolloverTarget(
        2, ProjectId("river"),
        HandoffDestination(destination, "feature", "/abs/next.md", "Task 1.1", None),
        "/abs/source.md", (True, 2, 1), "a" * 64, PermissionPreset.ASK,
    )  # fmt: skip


def _rollover(
    successor: SessionId,
    *,
    source: Path | None | object = _SAME,
    target: RolloverTarget | None = None,
    kind: RolloverKind = RolloverKind.WORKFLOW,
) -> Rollover:
    return Rollover(
        id="r1",
        handoff_id="h-0123456789abcdef0123",
        predecessor_session_id=SessionId.new(),
        successor_session_id=successor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        reason="workflow",
        plan=None,
        state=RolloverState.SUCCESSOR_STARTING,
        failure_code=None,
        failure_detail=None,
        requested_at=_AT,
        updated_at=_AT,
        source_root=_ORCHARD if source is _SAME else source,
        target=_target(_RIVER) if target is None else target,
        kind=kind,
    )


def test_a_cross_project_successor_is_granted_exactly_its_source_handoff_folder(
    projects: tuple[Path, Path],
) -> None:
    successor = SessionId.new()

    assert handoff_grant(_rollover(successor), successor) == (_ORCHARD / ".claude" / "handoffs",)


@pytest.mark.parametrize(
    "case",
    ["same-checkout", "source-inside-destination", "version-1", "no-source", "note", "other"],
)
def test_nothing_is_granted_where_the_ruling_does_not_reach(
    case: str, projects: tuple[Path, Path]
) -> None:
    successor = SessionId.new()
    rollover = {
        "same-checkout": _rollover(successor, target=_target(_ORCHARD)),
        "source-inside-destination": _rollover(
            successor, source=_RIVER / "sub", target=_target(_RIVER)
        ),
        "version-1": _rollover(successor, target=_target(_RIVER, version=1)),
        "no-source": _rollover(successor, source=None),
        "note": _rollover(successor, kind=RolloverKind.NOTE),
        "other": _rollover(SessionId.new()),
    }[case]

    assert handoff_grant(rollover, successor) == ()


async def test_the_grant_is_read_off_the_open_rollover_and_ends_with_it(
    tmp_path: Path, projects: tuple[Path, Path]
) -> None:
    from .test_permission_apply import _record

    store = SQLiteRolloverStore(open_database(tmp_path / "rows.sqlite3"))
    successor = SessionId.new()
    row = await store.open_for_ready(
        SessionId.new(),
        "h-0123456789abcdef0123",
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("codex"),
        plan="/abs/next.md",
        at=_AT,
        source_root=_ORCHARD,
        target=_target(_RIVER),
    )
    assert row is not None
    await store.advance(row.id, RolloverState.SUCCESSOR_STARTING, at=_AT)
    await store.record_successor(row.id, successor, at=_AT)
    grants = handoff_grants(store)

    assert await grants(_record(successor)) == (_ORCHARD / ".claude" / "handoffs",)
    assert await grants(_record(SessionId.new())) == ()

    await store.advance(row.id, RolloverState.FAILED, at=_AT, failure_code="adoption-timeout")
    assert await grants(_record(successor)) == ()


async def test_the_apply_pass_hands_the_grant_to_the_server_and_none_to_others() -> None:
    from .test_permission_apply import PROVIDER, SECOND, SESSION, THREAD, _record
    from .test_permission_apply import Rig as ApplyRig

    apply_rig = ApplyRig(_record(resumed_from=THREAD), _record(SECOND, resumed_from=THREAD))
    apply_rig.bindings.pending(SESSION, PermissionPreset.ASK)
    apply_rig.bindings.pending(SECOND, PermissionPreset.ASK)
    granted = (_ORCHARD / ".claude" / "handoffs",)

    async def roots(record):
        return granted if record.session_id == SESSION else ()

    apply_rig.apply_pass = PermissionApplyPass(
        apply_rig.bindings,
        sessions=apply_rig._sessions,
        appliers={str(PROVIDER): apply_rig.server},
        thread_of=apply_rig._thread_of,
        writable_roots=roots,
        now=lambda: apply_rig.now,
        sleep=apply_rig._sleep,
    )
    await apply_rig.tick()

    assert sorted(apply_rig.server.roots, key=len) == [(), granted]


async def test_a_grant_that_cannot_be_read_grants_nothing() -> None:
    from .test_permission_apply import PROVIDER, SESSION, THREAD, _record
    from .test_permission_apply import Rig as ApplyRig

    apply_rig = ApplyRig(_record(resumed_from=THREAD))
    apply_rig.bindings.pending(SESSION, PermissionPreset.ASK)

    async def broken(_record):
        raise OSError("the store went away")

    apply_rig.apply_pass = PermissionApplyPass(
        apply_rig.bindings,
        sessions=apply_rig._sessions,
        appliers={str(PROVIDER): apply_rig.server},
        thread_of=apply_rig._thread_of,
        writable_roots=broken,
        now=lambda: apply_rig.now,
        sleep=apply_rig._sleep,
    )
    await apply_rig.tick()

    assert apply_rig.server.roots == [()]


def _apply_rig_with(roots_of):
    from .test_permission_apply import PROVIDER, SESSION, THREAD, _record
    from .test_permission_apply import Rig as ApplyRig

    apply_rig = ApplyRig(_record(resumed_from=THREAD))
    apply_rig.bindings.pending(SESSION, PermissionPreset.ASK)

    async def roots(_record):
        return roots_of()

    apply_rig.apply_pass = PermissionApplyPass(
        apply_rig.bindings,
        sessions=apply_rig._sessions,
        appliers={str(PROVIDER): apply_rig.server},
        thread_of=apply_rig._thread_of,
        writable_roots=roots,
        now=lambda: apply_rig.now,
        sleep=apply_rig._sleep,
    )
    return apply_rig


async def test_a_grant_that_appears_after_an_apply_without_it_is_sent_once() -> None:
    """Review finding: the background pass applied before the rollover recorded its successor.
    The next look sees the grant it is owed and re-applies with it, once."""
    owed: list[tuple] = [()]
    apply_rig = _apply_rig_with(lambda: owed[0])
    await apply_rig.tick()
    assert apply_rig.server.roots == [()]

    owed[0] = (_ORCHARD / ".claude" / "handoffs",)
    await apply_rig.tick()
    await apply_rig.tick()

    assert apply_rig.server.roots == [(), owed[0]]


async def test_a_grant_whose_rollover_ended_is_withdrawn_by_an_apply_without_it() -> None:
    """Review finding: the thread keeps what it was last sent, so an ended rollover's grant is
    taken back by re-applying the preset with no root."""
    granted = (_ORCHARD / ".claude" / "handoffs",)
    owed: list[tuple] = [granted]
    apply_rig = _apply_rig_with(lambda: owed[0])
    await apply_rig.tick()
    assert apply_rig.server.roots == [granted]

    owed[0] = ()
    await apply_rig.tick()
    await apply_rig.tick()

    assert apply_rig.server.roots == [granted, ()]


async def test_an_unchanged_grant_is_never_sent_again() -> None:
    granted = (_ORCHARD / ".claude" / "handoffs",)
    apply_rig = _apply_rig_with(lambda: granted)
    for _ in range(4):
        await apply_rig.tick()

    assert apply_rig.server.roots == [granted]


def test_the_served_apply_pass_reads_the_grant_off_the_rollovers(tmp_path: Path) -> None:
    from remote_agents.adapters.agents.registry import provider_descriptors
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
    from remote_agents.composition.service import build_permission_apply_pass

    connection = open_database(tmp_path / "sessions.sqlite3")
    apply_pass = build_permission_apply_pass(
        store=SQLiteSessionStore(connection),
        descriptors=provider_descriptors(),
        activity_directory=tmp_path / "activity",
        connection=connection,
    )

    assert apply_pass._writable_roots is not None
    assert apply_pass._writable_roots.__qualname__.startswith("handoff_grants")


@pytest.mark.parametrize("trust_first", [False, True])
async def test_a_hook_review_that_outlasts_the_binding_wait_holds_not_fails(
    rig: Rig,  # noqa: F811
    trust_first: bool,
) -> None:
    """Stage 3 gate finding: the dialog is drawn during the binding's bounded wait, which gives
    up unbound. The rollover holds for the owner, then goes on once the dialog is answered."""
    bound = [False]

    async def ready(session_id, profile_id) -> Readiness:
        if not bound[0]:
            # Drawn while the binder waited, which then gave up.
            rig.owner_dialogs[session_id] = "a hook review"
        return Readiness(bound=bound[0])

    _gate(rig, ready)
    rig.ready()
    if trust_first:
        rig.launched_state = SessionState.UNTRUSTED
        await rig.run()
        _trust(rig)
    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert rollover.state is RolloverState.SUCCESSOR_STARTING, rollover.failure_code
    details = [e.detail for e in await rig.store.events(rollover.id)]
    assert OWNER_HOLD + "a hook review" in details
    assert rig.sends == [] and rig.reports == []

    rig.owner_dialogs.pop(rig.successor)  # answered in the pane
    bound[0] = True
    await rig.run(passes=2)

    assert rig.sends == [(rig.successor, TEMPLATE)]


@pytest.mark.parametrize("case", ["handoffs-link", "claude-link", "missing", "a-file"])
def test_a_handoff_folder_that_is_not_a_real_one_is_never_granted(
    case: str, projects: tuple[Path, Path], tmp_path: Path
) -> None:
    """Stage 3 gate finding: the predecessor writes its own checkout, so a `handoffs` (or
    `.claude`) it made a link would carry the grant to the link's target."""
    import shutil

    orchard, _river = projects
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    handoffs = orchard / ".claude" / "handoffs"
    if case == "handoffs-link":
        handoffs.rmdir()
        handoffs.symlink_to(elsewhere)
    elif case == "claude-link":
        shutil.rmtree(orchard / ".claude")
        (elsewhere / "handoffs").mkdir()
        (orchard / ".claude").symlink_to(elsewhere)
    elif case == "missing":
        handoffs.rmdir()
    else:
        handoffs.rmdir()
        handoffs.write_text("not a folder")
    successor = SessionId.new()

    assert handoff_grant(_rollover(successor), successor) == ()


def test_a_source_root_that_cannot_be_granted_still_gets_its_preset(
    projects: tuple[Path, Path], tmp_path: Path
) -> None:
    """A control character in a legal folder name: no grant, never an adapter refusal that
    would fail the whole preset (Stage 3 gate)."""
    odd = tmp_path / "odd\x07name"
    (odd / ".claude" / "handoffs").mkdir(parents=True)
    successor = SessionId.new()

    assert handoff_grant(_rollover(successor, source=odd), successor) == ()
