"""Keep each payload-identified hook's roster equal to the live sessions it may name (DEC-126).

A hook spawned by a provider's shared daemon inherits the environment of whichever pane started
that daemon, so it names its session from its payload against this roster instead. Who is on
the roster is lifecycle policy, so it is `notifiable`'s answer: the sessions an unprompted
notification could still be news about (DEC-031). A session that has ended is off the roster,
and its daemon's later events name nobody rather than naming it.

Published before every pane starts and once per activity pass. Before the pane, because a
session's provider thread is minted at launch and its first hook can fire seconds later; per
pass, because a session ending is noticed by reconciliation, not by a launch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from remote_agents.application.session_actions import notifiable
from remote_agents.domain.models import SessionState
from remote_agents.ports.hook_roster import HookRosterWriter, RosterSession
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.terminal import SessionFolderLookup

_LIVE = tuple(state for state in SessionState if notifiable(state))

#: How many times one publish rewrites a roster the store moved under. A launch a second never
#: makes it loop more than once; the bound is for a store that never settles.
_MAXIMUM_REWRITES = 3


class HookRosterPublisher:
    """Write one roster per payload-identified hook, from the store, every time it is asked."""

    def __init__(
        self,
        store: SessionStore,
        folder_for: SessionFolderLookup,
        hooks: Mapping[str, str],
        writer: HookRosterWriter,
    ) -> None:
        """`hooks` maps a profile id to the hook name whose roster its sessions go on."""
        self._store = store
        self._folder_for = folder_for
        self._hooks = dict(hooks)
        self._writer = writer
        self._lock = asyncio.Lock()

    async def publish(self) -> None:
        """Rewrite every roster, an empty one included: an empty roster names nobody.

        A hook whose provider has no roster falls back to its environment, so a roster is
        written even with no session on it -- once the service has spoken, the inherited
        variable is never trusted again.

        **Read again after writing, and written again if the store moved.** Publishers race:
        the activity pass, a launch in this process, and the other process's launches. One that
        listed the store just before a launch saved its record can land its older list after
        the launch's own publish, leaving the new session off until the next pass -- with its
        first hooks naming nobody, or an older session in the same folder. The lock orders the
        publishers in this process; the re-read is what covers the other process, because
        whichever publisher writes last has then seen the store as it stood after its write.
        """
        if not self._hooks:
            return
        async with self._lock:
            rosters = await self._rosters()
            for _ in range(_MAXIMUM_REWRITES):
                for hook, sessions in rosters.items():
                    await asyncio.to_thread(self._writer.write, hook, sessions)
                current = await self._rosters()
                if current == rosters:
                    return
                rosters = current

    async def _rosters(self) -> dict[str, tuple[RosterSession, ...]]:
        rosters: dict[str, list[RosterSession]] = {name: [] for name in self._hooks.values()}
        for record in await self._store.list(_LIVE):
            hook = self._hooks.get(str(record.profile_id))
            if hook is None:
                continue
            try:
                folder = await self._folder_for(record.session_id, record.project_id)
            except KeyError:
                continue
            if folder is None:
                continue
            rosters[hook].append(
                RosterSession(
                    str(record.session_id),
                    folder,
                    record.created_at,
                    record.resume_source_id,
                )
            )
        return {hook: tuple(sessions) for hook, sessions in rosters.items()}
