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
