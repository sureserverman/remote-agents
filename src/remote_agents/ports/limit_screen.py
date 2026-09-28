"""How an agent says a usage limit stopped it: the lines to look for, and what its words name.

A provider fact, declared by the provider's own vertical (DEC-070) and read by two callers: the
activity pass, which classifies a limit stop with the provider's sentence when its reading cannot
name the window, and the limit-screen watch, which finds the stop on the pane of an agent that
publishes no limit event at all. Neither caller spells a provider string; both ask this.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from remote_agents.ports.agent_activity import LimitHit


@dataclass(frozen=True, slots=True)
class LimitScreen:
    """One agent's limit sentence, as its vertical measured it.

    `markers` are regular expressions for the line that says the agent was stopped, matched
    against the agent's last output only (a marker quoted higher up the conversation is not a
    stop). `hint` reads the provider's own sentence -- the hook's detail, or the matched screen
    -- and answers the window and reset instant it names, or `None` when the text is not a limit
    stop at all. It is handed `now` so that a time-of-day can be placed on the right date, and it
    never raises: a sentence it cannot fully read answers what it could read.
    """

    markers: tuple[str, ...]
    hint: Callable[[str, datetime], LimitHit | None]
