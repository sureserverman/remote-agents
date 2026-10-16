"""The restart-resume switch: what it is called, and what each state is called.

`rollover_setting.py`'s sibling. Both surfaces draw this row -- the terminal's Settings screen
and the bot's `/settings` -- so the words live here once (DEC-007, DEC-091), and each surface
assembles its own row around them (DEC-043).
"""

from __future__ import annotations

#: What the switch is called on both surfaces.
RESTART_RESUME_TITLE = "Resume after restart"

#: What each state is called. On is the default.
RESTART_RESUME_LABELS: dict[bool, str] = {True: "on", False: "off"}
