"""Claude's stored Remote Control default, spoken as one key in `claude`'s own settings file.

The provider half of `ports/remote_control_default.py`. It is the sibling of
`adapters/agents/codex/remote_control.py` and the comparison is the quickest way to see what
this module is: both reach into a file the provider wrote for itself, because in neither case
does the provider offer anything better. Codex persists `remoteControlEnabled` under
`$CODEX_HOME/app-server-daemon/settings.json`; Claude resolves `remoteControlAtStartup` from its
user-scope settings file at every start. Neither has a read-only status verb worth calling.

**Why this project writes another tool's configuration file, stated rather than assumed.** The
owner asked for a control that turns Claude's Remote Control on, off, or leaves it to Claude.
Only two of those three are reachable from a launch: `--remote-control <name>` forces it *on*
and there is no flag that turns it off, measured
(`docs/acceptance-2026-09-11-surface-refresh.md` section 8, arms E-H). The off-switch Claude
does have is this key, at user scope, which is also what its own `/config` row *"Enable Remote
Control for all sessions"* writes. So the honest implementation drives the same key in the same
file -- and a value of our own kept elsewhere would be a second source able to disagree with
the one `claude` actually reads.

**It is not a new writer.** `adapters/agents/hook_settings.py` has edited this exact file since
the activity hooks shipped, with an exact-formatting round trip, a stale-read refusal, an atomic
replace and a preserved mode. This module adds one key to that machinery. The careful parts are
not re-implemented here, and a refusal from them is turned into a log line rather than swallowed
or raised, because the port promises a surface it will never raise.

**Reading and writing are deliberately asymmetric**, and that is the port's contract rather than
an accident here: a read forgives everything and answers `PROVIDER_DEFAULT`, because a hand-edited
settings file may not stop a Settings row from drawing; a write refuses anything it cannot do
exactly, because the file holds this project's own hooks and the owner's permission grants.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from remote_agents.adapters.agents.hook_settings import (
    HookInstallError,
    clear_settings_key,
    read_settings_document,
    set_settings_key,
)
from remote_agents.domain.models import ProfileId
from remote_agents.domain.remote_control import RemoteControlDefault

_LOG = logging.getLogger(__name__)

#: `claude`'s own name for the setting, from its settings schema: *"Start Remote Control bridge
#: automatically each session"*. Spelled here once and pinned by a test, because a typo would be
#: a row that reads and writes a key nothing consumes, with no symptom but the feature not
#: working.
REMOTE_CONTROL_AT_STARTUP_KEY = "remoteControlAtStartup"

#: The two values `claude`'s schema accepts, and the state each means. A key holding anything
#: else -- the number `1`, the string `"true"`, `null` -- is a file that does not answer the
#: question, so it reads as the provider default rather than as a guess at what was meant.
#: Compared with `is`, never `==`: `1 == True` in Python, and `1` is not a boolean `claude` would
#: accept.
_STORED: tuple[tuple[bool, RemoteControlDefault], ...] = (
    (True, RemoteControlDefault.ON),
    (False, RemoteControlDefault.OFF),
)


class ClaudeRemoteControlDefault:
    """Read and write `remoteControlAtStartup` in the settings file `claude` reads at startup.

    **Takes the file, and does not work out where it is.** Resolving it lives with
    `registry.claude_remote_control_default`, beside `default_settings_path`, which is where this
    project already decides where each agent keeps its configuration -- and it has to be there
    rather than here, because a vertical importing the registry to ask is a cycle: the registry
    imports the vertical (it is the only module allowed to). The first version of this class did
    exactly that and the import graph said so immediately.

    What is left here is the part that is genuinely Claude's: the key's name, the two values its
    schema accepts, and what their absence means.
    """

    profiles = frozenset({ProfileId("claude")})
    """Which launches this stored default governs, declared by the provider that owns it.

    Read by the composition root to wire `SessionService.launch`. **Declared here rather than
    named there**, on `ClaudeUsageReader.profiles`'s precedent and for the same two reasons: a
    profile id outside its own provider package is exactly what
    `test_a_provider_lives_in_one_package` refuses, and the question "which agents does
    `remoteControlAtStartup` decide for" is Claude's to answer, not the root's.

    A frozenset because the answer has been plural before: while `claude-remote` existed, this
    key governed both ids -- the same binary under two curated names. It is one now, and the
    type is what let that change without touching the caller.
    """

    def __init__(self, settings_path: Path) -> None:
        self._settings_path = settings_path

    @property
    def settings_path(self) -> Path:
        """Which file this port is about, so a surface can name it and a test can pin it."""
        return self._settings_path

    async def read(self) -> RemoteControlDefault:
        """The stored default, or `PROVIDER_DEFAULT` for every way reading can fail."""
        document = await asyncio.to_thread(read_settings_document, self._settings_path)
        stored = document.get(REMOTE_CONTROL_AT_STARTUP_KEY)
        for value, state in _STORED:
            if stored is value:
                return state
        return RemoteControlDefault.PROVIDER_DEFAULT

    async def write(self, value: RemoteControlDefault) -> None:
        """Record the choice where `claude` reads it, or log why it could not be recorded.

        `PROVIDER_DEFAULT` removes the key rather than writing a third value: `claude` has no
        spelling for "decide for yourself" other than the key's absence, and inventing one would
        put this project's idea of *unset* into somebody else's schema.
        """
        try:
            await asyncio.to_thread(self._write, value)
        except HookInstallError as error:
            # Every refusal the shared machinery makes arrives here: a file whose formatting
            # cannot be reproduced, one that changed under us, an absent `~/.claude`, an
            # unwritable directory. Each is a forgotten press and an untouched file, which the
            # next `read` reports honestly -- so the surface re-renders the truth rather than
            # the value the owner just chose. Warning rather than raising because the port
            # promises a screen that has no branch for a traceback.
            _LOG.warning(
                "could not store the Claude Remote Control default in %s: %s",
                self._settings_path,
                error,
            )
        except OSError:
            # Belt and braces for a failure mode the machinery converts but a future edit to it
            # might not: `read()` has the same pair in `read_settings_document`.
            _LOG.warning(
                "could not store the Claude Remote Control default in %s",
                self._settings_path,
                exc_info=True,
            )

    def _write(self, value: RemoteControlDefault) -> None:
        """The blocking half, so `write` can hand it to a thread and keep the loop free."""
        if value is RemoteControlDefault.PROVIDER_DEFAULT:
            clear_settings_key(self._settings_path, REMOTE_CONTROL_AT_STARTUP_KEY)
            return
        for stored, state in _STORED:
            if value is state:
                set_settings_key(self._settings_path, REMOTE_CONTROL_AT_STARTUP_KEY, stored)
                return
        # Unreachable through the enum, and a `ValueError` rather than a silent no-op for the
        # reason `next_remote_control_default` raises one: this project runs no type checker, so
        # a string arriving from a callback payload is a value rather than a diagnostic.
        raise ValueError(
            f"{value!r} is not a Remote Control default this project can store -- "
            f"one of {[member.value for member in RemoteControlDefault]}"
        )
