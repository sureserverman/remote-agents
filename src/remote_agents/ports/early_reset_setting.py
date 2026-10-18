"""Whether an early limits reset is told, and for how long, as a stored choice.

`ports/restart_resume_setting.py`'s shape: a stored intention in this project's own
`config.toml` (`limits.early_reset_notice`), read afresh and written through the one config
writer (DEC-088). One of `off`, `1h`, `6h`; `1h` by default.

**Total on both verbs.** `read` answers the default for a file that is absent or malformed,
and never raises. A failed `write` is one log line, and the read-back is what reports it.
"""

from __future__ import annotations

from typing import Protocol


class EarlyResetSettingPort(Protocol):
    """What the stored early-reset notice choice can be asked."""

    async def read(self) -> str:
        """The choice as the file states it now, or the default for every way reading fails."""
        ...

    async def write(self, value: str) -> None:
        """Record the owner's choice. Never raises; the read-back reports a refused write."""
        ...
