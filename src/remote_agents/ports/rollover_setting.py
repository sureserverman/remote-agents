"""Whether a session is rolled over to a fresh one when its workflow hands off, as a switch.

`ports/resume_setting.py`'s shape: a stored intention in this project's own `config.toml`
(`rollover.auto_rollover`), read afresh and written through the one config writer (DEC-088).
Off by default -- a rollover launches and stops sessions unattended, so the owner turns it on
(DEC-115).

**Total on both verbs.** `read` answers off for a file that is absent, malformed or holding a
non-bool, and never raises. Off is the default and the safe answer at once, so the Settings
rows and the rollover pass share this one read: the pass asks it on every run, which is what
lets a flip land without a restart. A failed `write` is one log line, and the read-back is
what reports it.
"""

from __future__ import annotations

from typing import Protocol


class RolloverSettingPort(Protocol):
    """What the stored auto-rollover switch can be asked."""

    async def read(self) -> bool:
        """The switch as the file states it now, or off for every way reading fails."""
        ...

    async def write(self, value: bool) -> None:
        """Record the owner's choice. Never raises; the read-back reports a refused write."""
        ...
