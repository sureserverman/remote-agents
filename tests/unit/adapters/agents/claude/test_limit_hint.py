"""Claude's own limit sentence, read for the window it names and the instant it resets.

Every sentence below is either one on record in the owner's `agent_activity` table or built from
the bundle's own formatter (`docs/acceptance-2026-09-28-limit-screens.md`): `You've hit your
<label>` with `bme` naming the label, then ` · resets <time> (<zone>)`, where the time is
`10:50am` inside a day and `Sep 30, 9am` beyond one, with the year added when it differs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from remote_agents.adapters.agents.claude.limit_screen import LIMIT_SCREEN
from remote_agents.ports.agent_activity import LimitHit

_LONDON = ZoneInfo("Europe/London")
# 08:35 in London on 2026-09-24 (BST), a few minutes after one of the recorded stops.
_NOW = datetime(2026, 9, 24, 7, 35, 46, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "You've hit your session limit · resets 10:50am (Europe/London)",
            LimitHit("5h", datetime(2026, 9, 24, 10, 50, tzinfo=_LONDON)),
            id="on-record-morning",
        ),
        # Recorded at 19:06 UTC on the 23rd; 10:10pm London is later the same evening.
        pytest.param(
            "You've hit your session limit · resets 10:10pm (Europe/London)",
            LimitHit("5h", datetime(2026, 9, 24, 22, 10, tzinfo=_LONDON)),
            id="on-record-evening-same-day",
        ),
        # A time already past today names tomorrow's.
        pytest.param(
            "You've hit your session limit · resets 3am (Europe/London)",
            LimitHit("5h", datetime(2026, 9, 25, 3, 0, tzinfo=_LONDON)),
            id="a-past-time-is-tomorrow",
        ),
        pytest.param(
            "You've hit your weekly limit · resets Sep 30, 9am (Europe/London)",
            LimitHit("week", datetime(2026, 9, 30, 9, 0, tzinfo=_LONDON)),
            id="weekly-with-a-date",
        ),
        pytest.param(
            "You've hit your Opus limit · resets Oct 2, 2027, 9:30am (Europe/London)",
            LimitHit("opus week", datetime(2027, 10, 2, 9, 30, tzinfo=_LONDON)),
            id="a-model-week-with-a-year",
        ),
        pytest.param(
            "You've hit your session limit · resets 3pm (Europe/London) · progress saved",
            LimitHit("5h", datetime(2026, 9, 24, 15, 0, tzinfo=_LONDON)),
            id="progress-saved-suffix",
        ),
        pytest.param(
            "You've hit your session limit",
            LimitHit("5h", None),
            id="no-reset-named",
        ),
        # A zone this host cannot resolve keeps the window and drops an instant it would
        # otherwise have to guess.
        pytest.param(
            "You've hit your weekly limit · resets Sep 30, 9am (Mars/Olympus_Mons)",
            LimitHit("week", None),
            id="unknown-zone",
        ),
        pytest.param(
            "You've hit your usage credit limit · resets 3pm (Europe/London)",
            LimitHit(None, datetime(2026, 9, 24, 15, 0, tzinfo=_LONDON)),
            id="a-limit-no-window-names",
        ),
    ],
)
def test_claude_names_its_window_and_reset(text: str, expected: LimitHit) -> None:
    assert LIMIT_SCREEN.hint(text, _NOW) == expected


@pytest.mark.parametrize(
    "text",
    ["Ran the suite.", "", "The API rate-limited us; retrying."],
)
def test_a_sentence_that_is_not_a_limit_stop_says_nothing(text: str) -> None:
    assert LIMIT_SCREEN.hint(text, _NOW) is None
