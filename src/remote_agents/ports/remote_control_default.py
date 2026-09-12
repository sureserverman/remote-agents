"""The stored-intention boundary: whether a provider's next session starts connected.

A third Remote Control port, and the three are not alternatives to each other -- each has a
different subject and none can answer for another:

- `ports/terminal.py` owns Claude's **pane** toggle: one live session, read by capturing it.
- `ports/host_remote_control.py` owns Codex's **daemon** enrollment: this machine's link to
  OpenAI's relay, read over a socket.
- this port owns a provider's **stored default**: what the owner has asked for next time,
  read from a file the provider itself reads at startup.

The distinction that matters for the third is that nothing it reports is about a session that
exists. A surface rendering it is rendering an intention, so the reading can be acted on with
no pane open and no daemon running -- which is why it takes no session id and no host, and why
it is the only one of the three whose `write` is a plain assignment rather than a drive.

**Why a port rather than a settings file of this project's own.** The value lives in the
provider's file because the provider is what reads it: Claude resolves
`remoteControlAtStartup` from `~/.claude/settings.json` at every start, and a copy of that
value in a file of ours would be a second source able to disagree with the one that decides.
The premise check measured the disagreement being unfixable from this side -- the launch flag
that would implement a local copy's *off* instead overrides the file
(`docs/acceptance-2026-09-11-surface-refresh.md` section 8) -- so the port writes where the
provider looks. Which file that is, and how the key is spelled, is adapter knowledge and lives
in exactly one module per provider (ARCH-02).

**Total, like every preference boundary in this project.** An implementation answers
`PROVIDER_DEFAULT` for a file that is absent, empty, unreadable, malformed or carrying a value
this version does not know, and never raises for reading one: a settings file the owner edited
by hand may not be a reason a screen will not draw (DEC-053's rule, and
`adapters/tui/preferences.py`'s). A failed *write* is likewise one log line and not an
exception -- the cost is a forgotten choice, and the surface re-reads and shows the truth.
"""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.remote_control import RemoteControlDefault


class RemoteControlDefaultPort(Protocol):
    """What one provider's stored Remote Control default can be asked.

    Two verbs and deliberately not a third. There is no "clear" beside `write`, because
    clearing is what writing `PROVIDER_DEFAULT` *means*: a provider whose unset key resolves
    to its own default has no separate empty state to reach, and a port offering both would
    let two spellings of one intention drift apart in the adapters.
    """

    async def read(self) -> RemoteControlDefault:
        """This provider's stored default, or `PROVIDER_DEFAULT` for every way reading fails.

        Never raises. A surface calls this to draw a row, and a row has one branch for a
        reading and none for a traceback -- the same contract `HostRemoteControl.status`
        carries for the same reason.
        """
        ...

    async def write(self, value: RemoteControlDefault) -> None:
        """Record the owner's choice where the provider will read it.

        `PROVIDER_DEFAULT` **removes** the stored value rather than writing a third one, so
        the provider resolves its own default afresh. Never raises: a write that could not
        land is a forgotten choice, which the next `read` will report honestly.
        """
        ...
