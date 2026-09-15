"""The limits-source switch as a capability, over `config`'s reader and its one writer.

**Why this is in `composition/` and not beside Claude's other adapters.** The obvious home is
`adapters/agents/claude/limits_source.py`, next to the reader router that consults this switch
on every account read -- and that module may not import `remote_agents.config`, which is where
both the reader and the writer live. `check_imports.py` enforces it: only the two *driver*
adapters (`telegram`, `tui`) may reach the package root, and `agents` is not one of them.
Sub-plan 01 met the same wall on the read side and answered it the same way, by handing the
adapter a `partial(read_claude_limits_source, path)` from the root rather than letting the
package open a file. This is that answer for the write side, given a name and a shape.

So the rule it observes is DEC-015's, not a workaround of it: composition is where a file path
and an adapter are allowed to meet, and the `composition` package is an enumerated member of
the closed set that may do it.

**Total on both verbs, which is the port's contract and not a courtesy.** `write_limits_key`
refuses in more shapes than any other writer in this project -- a file that does not parse, a
missing `[limits]` table, a value outside the closed set, a file changed since it was read, a
`[limits]` value whose continuation lines cannot be re-scanned -- and every one of those is
something an owner can produce by hand-editing their own config. None of them may reach a
screen as a traceback. The Settings row finds out the same way it finds out about a refused
Claude write: it reads back, and its intention is not there.

**Both verbs go through `asyncio.to_thread`.** The read is a small parse, but the write is a
read, a re-parse of its own output, an fsync and a rename, and neither surface may block its
event loop on the disk during a keypress -- the same rule `Backend.limits` observes for the
directory sweep behind it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from remote_agents.config import (
    ConfigError,
    read_claude_limits_source,
    write_limits_key,
)

_LOG = logging.getLogger(__name__)

#: The one key this setting owns. Spelled once here so the writer and the reader cannot be
#: pointed at different keys by a later edit.
_KEY = "claude_limits_source"


class ConfigLimitsSource:
    """`ports.limits_source.LimitsSourcePort` over the operator's `config.toml`."""

    def __init__(self, path: Path) -> None:
        #: The file the config was loaded from, so a flip lands where the selector reads.
        #: `AppConfig.path` where there is one and the production default where there is not
        #: -- the caller resolves that, for the reason the read side already does.
        self._path = path

    async def read(self) -> str:
        """What the file says now, read afresh rather than remembered.

        No cache, deliberately. The bot and the terminal are separate processes over one file
        (DEC-005's accepted multi-writer world), so a value held from the last read is a value
        the other surface may have changed -- and this row's whole job is to say what the
        selector will actually consult on its next account read.
        """
        return await asyncio.to_thread(read_claude_limits_source, self._path)

    async def write(self, value: str) -> None:
        """Record the choice, or log why it could not be recorded. Never raises.

        `ConfigError` is caught by name because it is the refusal the writer is *designed* to
        make -- the owner's file is in a shape this project declines to rewrite -- and it is
        logged at warning, since a flip the owner asked for and did not get is worth a line.
        The broad clause under it is for the shapes `write_limits_key` cannot promise about
        (a path that became a directory, a full disk); those are debug, because the row is
        already about to tell the owner by reading back.
        """
        try:
            await asyncio.to_thread(write_limits_key, self._path, _KEY, value)
        except ConfigError as error:
            _LOG.warning("the limits source could not be written to %s: %s", self._path, error)
        except Exception:
            _LOG.debug("the limits source could not be written", exc_info=True)
