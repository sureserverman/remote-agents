"""Which source Claude's account-wide limits are read from, as a thing a surface can change.

The fourth boundary in the Remote Control family's shape but not its subject: like
`ports/remote_control_default.py` this is a *stored intention* read from a file, and like it
the read takes no session and the write is a plain assignment. What differs is whose file it
is. That port writes where the **provider** looks, because Claude resolves
`remoteControlAtStartup` itself and a copy of ours would be a second source free to disagree
with the one that decides. This one writes where **this project** looks:
`limits.claude_limits_source` in `config.toml` is read by our own limits selector on every
account read, and no other program has an opinion about it.

**Why the value is a bare `str`.** The two legal spellings are `config.CLAUDE_LIMITS_SOURCES`,
and a port may not import the package root -- nor should it, since which words the operator's
schema accepts is configuration knowledge and not a domain fact. An enum here would be a third
place the pair is written down, after the config's tuple and the application's label table,
and the one that could silently fall behind. `read` is total over whatever the file holds, so
a caller never has to handle a value that is not in the set: it gets the default instead.

**Total, like every preference boundary in this project.** An implementation answers the
default for a file that is absent, empty, unreadable, malformed or naming a source this build
does not know, and never raises for reading one. A failed *write* is one log line and not an
exception -- the cost is a forgotten choice, which the next `read` reports honestly, and the
Settings row detects the refusal by comparing the read-back against what the press intended.
That contract matters more here than on the sibling ports, because `write_limits_key` refuses
on purpose in several shapes the owner can create by hand: a `[limits]` table it cannot
re-scan, a file changed since it was read, a commented-out key line.
"""

from __future__ import annotations

from typing import Protocol


class LimitsSourcePort(Protocol):
    """What the stored choice of limits source can be asked.

    Two verbs and deliberately not a third, for `RemoteControlDefaultPort`'s reason: there is
    no "clear" beside `write`, because every legal value is a real choice and the absence of
    the key already means the default. A port offering both would let two spellings of one
    intention drift apart in the implementations.
    """

    async def read(self) -> str:
        """The source the file names right now, or the default for every way reading fails.

        Never raises. A surface calls this to draw a row, and a row has one branch for a
        reading and none for a traceback.
        """
        ...

    async def write(self, value: str) -> None:
        """Record the owner's choice where this project's limits selector will read it.

        Never raises, including for the shapes the writer refuses by design. A write that
        could not land is a forgotten choice, and the read-back is what reports it.
        """
        ...
