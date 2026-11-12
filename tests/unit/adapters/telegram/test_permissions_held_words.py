"""Every surface that reports held automatic work words the permission reason itself.

A reason missing from a table is shown as its key, or as another reason's sentence. These pin
the four places a held session is reported: the rollover, the schedule, the restart nudge and
the after-limit nudge (OD-2).
"""

from __future__ import annotations

from remote_agents.adapters.telegram import (
    notifications,
    restart_notifications,
    schedule_notifications,
)
from remote_agents.application.limit_resume import NotResumed
from remote_agents.application.permission_apply import PERMISSIONS_HELD


def test_every_held_report_has_permission_words_of_its_own() -> None:
    schedule = schedule_notifications._NOT_TYPED[PERMISSIONS_HELD]
    restart = restart_notifications._WHY_NOT_NUDGED[PERMISSIONS_HELD]
    limit = notifications._NOT_RESUMED_WORDS[NotResumed.PERMISSIONS]

    for words in (schedule, restart, limit):
        assert "permissions could not be applied" in words
    assert NotResumed.PERMISSIONS.value == PERMISSIONS_HELD
    assert set(notifications._NOT_RESUMED_WORDS) == set(NotResumed), (
        "a reason without words falls back to another reason's sentence"
    )
