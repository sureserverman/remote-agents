"""Codex's limit sentence: the stop it prints into its pane, and the retry instant it names.

Read out of the installed binary (`docs/acceptance-2026-09-28-limit-screens.md`, codex-cli
0.158.0): `You’ve hit your usage limit.` -- a U+2019 apostrophe -- optionally followed by a way
round it (another model, an upgrade, the usage page) and by ` Try again at <time>.` or
` or try again at <time>.`, the time formatted `%b %-d, %Y %-I:%M %p` in the host's own zone.
Codex fires no hook on a failed turn, so this sentence on the pane is the only place its stop is
visible; the limit-screen watch matches it there.

**Both apostrophes are matched.** The binary spells U+2019, and the one live sighting on record
was transcribed with an ASCII one; a marker that matched only one would miss the other silently.

**The sentence never names the window.** Only the reading can, so the hint answers `None` for it
and keeps the retry instant, which is still the one the lift waits for.
"""

from __future__ import annotations

import re
from datetime import datetime

from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.limit_screen import LimitScreen

#: Anchored at the start of a line, after nothing but glyphs (Codex draws `■ `), so a sentence
#: the owner typed or an answer quoted mid-line is not a stop.
_STOP = r"^\W*You[’']ve hit your usage limit"
_RETRY = re.compile(r"[Tt]ry again at (?P<when>[A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)")


def _hint(text: str, now: datetime) -> LimitHit | None:  # noqa: ARG001 -- the instant is dated
    if re.search(_STOP, text, re.MULTILINE) is None:
        return None
    found = _RETRY.search(text)
    if found is None:
        return LimitHit(None, None)
    try:
        # A naive local reading placed in the host's zone: Codex formats it with the host's
        # clock, and `astimezone` on a naive value is exactly "this is local time".
        return LimitHit(
            None, datetime.strptime(found.group("when"), "%b %d, %Y %I:%M %p").astimezone()
        )
    except ValueError:
        return LimitHit(None, None)


LIMIT_SCREEN = LimitScreen(markers=(_STOP,), hint=_hint)
