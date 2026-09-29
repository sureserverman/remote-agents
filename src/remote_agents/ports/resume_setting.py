"""Whether a limit-stopped session is sent the resume nudge after its limit lifts, as a switch.

`ports/limits_source.py`'s shape with a bool: a stored intention in this project's own
`config.toml` (`limits.resume_after_limit`), read afresh and written through the one
`[limits]` writer (DEC-088). On by default -- the owner asked for the nudge unless they
switch it off.

**Total on both verbs.** `read` answers the default for a file that is absent, malformed or
holding a non-bool, and never raises: the service consults it on every lift, and the Settings
rows draw it. A failed `write` is one log line, and the read-back is what reports it.
"""

from __future__ import annotations

from typing import Protocol


class ResumeSettingPort(Protocol):
    """What the stored resume switch can be asked."""

    async def read(self) -> bool:
        """The switch as the file states it now, or on for every way reading fails."""
        ...

    async def write(self, value: bool) -> None:
        """Record the owner's choice. Never raises; the read-back reports a refused write."""
        ...
