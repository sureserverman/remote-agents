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

    async def publish(self) -> None:
        """Rewrite every roster, an empty one included: an empty roster names nobody.

        A hook whose provider has no roster falls back to its environment, so a roster is
        written even with no session on it -- once the service has spoken, the inherited
        variable is never trusted again.
        """
        if not self._hooks:
            return
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
        for hook, sessions in rosters.items():
            await asyncio.to_thread(self._writer.write, hook, tuple(sessions))
