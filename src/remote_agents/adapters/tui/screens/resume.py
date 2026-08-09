"""Resuming a saved conversation: project, agent, one bounded page, confirm.

Four screens replacing the last four `Step` members, and with them the last four navigation
fields. `_resume_project`, `_resume_profile` and `_resume_choice` are constructor arguments
on the screens that need them, and `_resume_page` / `_resume_page_count` are local to the one
screen that pages — which is the whole of what "screen-local paging state" buys: a second
entry point firing mid-navigation can no longer reset a value the screen under it is using.

Honors DEC-002: which agents are offered comes from `conversations.capabilities()`, which
reports what each provider can actually do on this host, and never from a version allowlist.

**Two more back-path shortcuts are deliberately gone**, matching the pairs Tasks 2.1 and 2.2
removed. Escape at the agent choice used to jump straight to the project list, skipping the
resume project choice; and the confirm's own Cancel row used to restart the whole flow while
Escape from the same position went back only one — the two disagreed. Both are one level now,
so Cancel and Escape finally mean the same thing here.
`test_back_out_of_the_resume_flow_stops_at_every_position` is that behaviour.

Each fetch happens on the screen the owner is *leaving*, and the next screen is pushed only
once it succeeds. That is deliberate and it is the behaviour the hand-rolled chain had: a
capability read that fails reports onto the position that asked for it, rather than opening
an empty screen carrying an error the owner would have to leave in order to read.
"""

from __future__ import annotations

import logging

from remote_agents.adapters.tui.model import (
    _BACK,
    _CANCEL,
    _NEXT,
    _PREVIOUS,
    conversation_row,
)
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.conversations import ConversationCatalogueQuery
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId

_LOG = logging.getLogger(__name__)
_RESUME_PAGE_SIZE = 10


class ResumeProjectsScreen(ChoiceScreen):
    """The project whose saved conversations the owner wants to reopen."""

    position = "RESUME_PROJECTS"
    status = "Resume a conversation. Choose its project."

    async def populate(self) -> None:
        self.hide_entry()
        self.show_choices(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}")
                for project in self.tui.catalogue
            )
            or ((_CANCEL, "No projects available"),)
        )

    async def choose(self, key: str) -> None:
        project = next((item for item in self.tui.catalogue if item.opaque_id == key), None)
        if project is None:
            # Stay in the resume flow, as the launch picker does for the same failure,
            # rather than dropping the owner into a different wizard with no explanation.
            self.set_status("That project is no longer available. Refresh and try again.")
            return
        conversations = self.services.conversations
        if conversations is None:
            return
        tui = self.tui
        # Guarded across the read for the reason the hand-rolled flow established: a second
        # entry point firing mid-navigation used to reset the chosen project, after which
        # selecting a profile silently did nothing and only Escape recovered.
        tui.set_busy(True)
        try:
            try:
                capabilities = await conversations.capabilities()
            except Exception as error:
                _LOG.exception("resume capabilities failed")
                self.set_status(f"Resume is unavailable: {error}")
                self.show_choices(((_BACK, "Back"),))
                return
            capable = tuple(
                capability
                for capability in capabilities
                if capability.catalogue_available and capability.selected_resume_available
            )
            # Inside the guard, not after it. `push_screen` yields while the new screen
            # mounts, so clearing first leaves a window in which a second global binding
            # pops the screen being mounted and the fetched capabilities are discarded with
            # no error — the same "a second entry point mid-navigation" class the guard
            # exists for, just failing silently instead of stranding.
            await self.app.push_screen(ResumeProfilesScreen(project, capable))
        finally:
            tui.set_busy(False)


class ResumeProfilesScreen(ChoiceScreen):
    """Only the agents that report themselves resume-capable on this host (DEC-002)."""

    position = "RESUME_PROFILES"

    def __init__(
        self, project: CatalogProject, capable: tuple[ProfileResumeCapability, ...]
    ) -> None:
        super().__init__()
        self.project = project
        self.capable = capable

    async def populate(self) -> None:
        self.hide_entry()
        if not self.capable:
            self.set_status("No agent on this host can resume a saved conversation.")
            self.show_choices(((_BACK, "Back"),))
            return
        self.set_status("Choose the agent whose conversation you want to resume.")
        self.show_choices(
            tuple((str(item.profile_id), str(item.profile_id)) for item in self.capable)
            + ((_BACK, "Back"),)
        )

    async def choose(self, key: str) -> None:
        if key == _BACK:
            self.app.pop_screen()
            return
        if not any(profile.profile_id == key for profile in self.services.profiles):
            # Defence in depth, matching the launch picker: the rows here are already
            # filtered to resume-capable profiles, so a key naming another one is stale.
            self.set_status("That agent is not available on this host.")
            return
        tui = self.tui
        tui.set_busy(True)
        try:
            page = await fetch_page(self, self.project, key, 1)
            if page is None:
                return
            # Held across the push for the reason given on `ResumeProjectsScreen.choose`.
            await self.app.push_screen(ResumeConversationsScreen(self.project, key, page))
        finally:
            tui.set_busy(False)


class ResumeConversationsScreen(ChoiceScreen):
    """One bounded page of safe metadata; provider IDs never leave the server."""

    position = "RESUME_CONVERSATIONS"

    def __init__(
        self, project: CatalogProject, profile: str, page: ConversationCataloguePage
    ) -> None:
        super().__init__()
        self.project = project
        self.profile = profile
        # The paging state the app used to carry as `_resume_page` / `_resume_page_count`.
        # Local to the one screen that pages, so nothing else can move it under this screen.
        self.page = page

    async def populate(self) -> None:
        self.hide_entry()
        self.render_page()

    def render_page(self) -> None:
        page = self.page
        if page.unavailable_reason is not None:
            self.set_status(f"Conversations are unavailable: {page.unavailable_reason}")
            self.show_choices(((_BACK, "Back"),))
            return
        if not page.conversations:
            self.set_status("There are no saved conversations for that agent and project.")
            self.show_choices(((_BACK, "Back"),))
            return
        entries = [(str(item.reference), conversation_row(item)) for item in page.conversations]
        if page.page > 1:
            entries.append((_PREVIOUS, "Previous page"))
        if page.page < page.page_count:
            entries.append((_NEXT, "Next page"))
        entries.append((_BACK, "Back"))
        self.set_status(f"Choose a conversation. Page {page.page} of {page.page_count}.")
        self.show_choices(tuple(entries))

    async def choose(self, key: str) -> None:
        conversations = self.services.conversations
        if conversations is None:
            return
        if key == _BACK:
            self.app.pop_screen()
            return
        if key in {_NEXT, _PREVIOUS}:
            await self.turn_page(1 if key == _NEXT else -1)
            return
        try:
            # The reference is only ever one this surface rendered from a server-issued
            # page; constructing it here re-validates its opaque shape, and resolution is
            # server-side, so a forged or stale value resolves to nothing rather than a path.
            resolved = await conversations.resolve_for_resume(ConversationReference(key))
        except ValueError:
            self.set_status("That conversation selection is not valid.")
            return
        except Exception as error:
            _LOG.exception("conversation resolve failed")
            self.set_status(f"That conversation could not be resolved: {error}")
            return
        if resolved is None:
            self.set_status("That conversation is no longer available.")
            return
        await self.app.push_screen(ResumeConfirmScreen(self.project, self.profile, resolved))

    async def turn_page(self, step: int) -> None:
        tui = self.tui
        wanted = max(1, min(self.page.page + step, self.page.page_count))
        tui.set_busy(True)
        try:
            page = await fetch_page(self, self.project, self.profile, wanted)
        finally:
            tui.set_busy(False)
        if page is None:
            return
        self.page = page
        self.render_page()


class ResumeConfirmScreen(ChoiceScreen):
    """The last position before a session is started, resting on Cancel.

    The abort entry is first, so it is what the cursor rests on and what a stray enter
    activates — the same mitigation every other confirmation in this surface carries.
    """

    position = "RESUME_CONFIRM"

    def __init__(
        self, project: CatalogProject, profile: str, resolved: ResolvedConversation
    ) -> None:
        super().__init__()
        self.project = project
        self.profile = profile
        self.resolved = resolved

    async def populate(self) -> None:
        self.hide_entry()
        self.set_status(
            f"Resume {conversation_row(self.resolved.summary)}\n"
            f"Agent: {self.profile}\n"
            "This starts a new managed session continuing that conversation."
        )
        self.show_choices(((_CANCEL, "Cancel"), ("resume-confirm", "Resume it")))

    async def choose(self, key: str) -> None:
        if key != "resume-confirm":
            self.app.pop_screen()
            return
        await self.tui.issue_resume(self, self.project, self.profile, self.resolved)


async def fetch_page(
    screen: ChoiceScreen, project: CatalogProject, profile: str, page: int
) -> ConversationCataloguePage | None:
    """Read one page, or report the failure onto `screen` and answer `None`.

    Shared by the two screens that fetch a page — the profile choice, which fetches the
    first, and the conversation list, which fetches every later one. Reporting onto the
    caller's screen is what keeps a failed read on the position that asked for it.
    """
    conversations = screen.services.conversations
    if conversations is None:
        return None
    try:
        return await conversations.catalogue(
            ConversationCatalogueQuery(
                profile_id=ProfileId(profile),
                project_id=ProjectId(project.opaque_id),
                page=page,
                page_size=_RESUME_PAGE_SIZE,
            )
        )
    except Exception as error:
        _LOG.exception("conversation catalogue failed")
        screen.set_status(f"The conversations could not be listed: {error}")
        screen.show_choices(((_BACK, "Back"),))
        return None
