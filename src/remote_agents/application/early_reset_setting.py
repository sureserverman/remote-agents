"""The early-reset notice choice: what it is called, what each state is called, and how long
each keeps its message.

`limits_source.py`'s shape: both surfaces draw this row -- the terminal's Settings screen and
the bot's `/settings` -- so the words live here once (DEC-007, DEC-091), and each surface
assembles its own row around them (DEC-043).

**The values are restated rather than imported**, for `limits_source.py`'s reason: `application/`
may not import `remote_agents.config` (ARCH-02). `test_early_reset_choices_match_config`
pins this module's set to `config.EARLY_RESET_NOTICES`.
"""

from __future__ import annotations

from datetime import timedelta

#: What the choice is called on both surfaces.
EARLY_RESET_TITLE = "Early limits reset notice"

#: What each state is called on screen.
EARLY_RESET_LABELS: dict[str, str] = {"off": "off", "1h": "for 1 hour", "6h": "for 6 hours"}

#: How long a sent message stays before it is deleted; `None` for a choice that sends none.
EARLY_RESET_LIFETIMES: dict[str, timedelta | None] = {
    "off": None,
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
}

#: The default, as `config.DEFAULT_EARLY_RESET_NOTICE` states it.
DEFAULT_EARLY_RESET_NOTICE = "1h"

_CYCLE: tuple[str, ...] = ("off", "1h", "6h")


def next_early_reset_notice(current: str) -> str:
    """What one press advances to from `current`, walking off, 1 hour, 6 hours, off.

    Total, as `next_limits_source` is: an unknown value advances from the default.
    """
    index = _CYCLE.index(current if current in _CYCLE else DEFAULT_EARLY_RESET_NOTICE)
    return _CYCLE[(index + 1) % len(_CYCLE)]


def early_reset_lifetime(choice: str) -> timedelta | None:
    """How long a message sent under `choice` stays; an unknown choice is the default's."""
    return EARLY_RESET_LIFETIMES.get(choice, EARLY_RESET_LIFETIMES[DEFAULT_EARLY_RESET_NOTICE])
