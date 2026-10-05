"""The auto-rollover switch: what it is called, and what each state is called.

`resume_setting.py`'s sibling. Both surfaces draw this row -- the terminal's Settings screen
and the bot's `/settings` -- so the words live here once (DEC-007, DEC-091), and each surface
assembles its own row around them (DEC-043).
"""

from __future__ import annotations

#: What the switch is called on both surfaces.
ROLLOVER_TITLE = "Roll over to a fresh session on handoff"

#: What each state is called. Off is the default.
ROLLOVER_LABELS: dict[bool, str] = {True: "on", False: "off"}
