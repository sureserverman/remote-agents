"""A rollover types its template only once the successor's thread is bound.

The template's adoption asks `session-id`, which refuses an unbound thread, so typing first would
only fail later, after a predecessor had waited out the adoption's patience. A successor that cannot
be bound fails the rollover by name, and its predecessor runs on.
"""

from __future__ import annotations

import pytest

from remote_agents.application.rollover import BINDING_FAILED, RolloverPass
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


def _binding(test_rig: Rig, answer: bool | Exception) -> list[tuple[object, object]]:  # noqa: F811
    """Give the rig's pass a binding that answers `answer`, and return who it was asked about."""
    asked: list[tuple[object, object]] = []

    async def bound(session_id, profile_id) -> bool:
        asked.append((session_id, profile_id, list(test_rig.sends)))
        if isinstance(answer, Exception):
            raise answer
        return answer

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
        bound=bound,
        graceful_stop=test_rig.graceful_stop,
        notify=test_rig.told,
        now=lambda: test_rig.clock[0],
        sleep=test_rig.sleep,
    )
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
