"""Whether interrupted sessions are resumed after a restart, as a switch.

`ports/resume_setting.py`'s shape: a stored intention in this project's own `config.toml`
(`restart.resume_interrupted`), read afresh and written through the one config writer
(DEC-088). On by default.

**Total on both verbs.** `read` answers the default for a file that is absent or malformed,
and never raises; it is the Settings rows' read. The startup pass does not go through this
port -- it reads the file itself with `read_resume_interrupted(when_unsure=False)`, so a
half-edited file resumes nothing. A failed `write` is one log line, and the read-back is what
reports it.
"""

from __future__ import annotations

from typing import Protocol


class RestartResumeSettingPort(Protocol):
    """What the stored restart-resume switch can be asked."""

    async def read(self) -> bool:
        """The switch as the file states it now, or the default for every way reading fails."""
        ...

    async def write(self, value: bool) -> None:
        """Record the owner's choice. Never raises; the read-back reports a refused write."""
        ...
