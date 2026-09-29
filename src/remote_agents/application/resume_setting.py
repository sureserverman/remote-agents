"""The resume-after-limit switch: what it is called, and what each state is called.

`limits_source.py`'s sibling. Both surfaces draw this row -- the terminal's Settings screen and
the bot's `/settings` -- so the words live here once (DEC-007, DEC-091), and each surface
assembles its own row around them (DEC-043).

The title says what the switch does rather than naming the nudge's text: the text is spelled
once, in `limit_resume.py`, where the service types it.
"""

from __future__ import annotations

#: What the switch is called on both surfaces.
RESUME_TITLE = "Resume after a limit lifts"

#: What each state is called. On is the default, so its label is the short one.
RESUME_LABELS: dict[bool, str] = {True: "on", False: "off"}
