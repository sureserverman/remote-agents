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
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.application.conversations import ConversationCatalogueQuery, resume_available
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

    #: Declared `NEVER_EMPTY` at first writing, on the reasoning that this screen is "reached
    #: only from a catalogue that had projects in it". Nothing enforces that: `action_resume`
    #: pushes this position without checking the catalogue, and a freshly configured host —
    #: or one whose catalogue read came back empty — reaches it with nothing to list. The
    #: original declaration was a claim about a precondition that does not exist.
    empty_state = "No projects in the catalogue to reopen a conversation in."

    position = "RESUME_PROJECTS"
    status = "Choose the project whose conversation you want to reopen."
    crumb = "Resume"
    #: This screen renders the catalogue, so Refresh means something here — and its own
    #: failure text below tells the owner to press it. Task 1.1's scope line reads "every
    #: screen that can be refreshed" and then names two in parentheses; this is a third, and
    #: the naming is illustrative rather than exhaustive. Left out, the next task disables the
    #: binding on `can_refresh = False` screens and the advice below would point at a key the
    #: footer no longer offers. Found by the Task 1.1 review.
    can_refresh = True

    async def refresh_contents(self) -> None:
        """Re-read the catalogue and redraw, as the launch picker does for the same rows."""
        if await self.tui.reload_catalogue():
            await self.populate()
            return
        self.announce("The project catalogue could not be re-read. Check this host.")

    async def populate(self) -> None:
        self.hide_entry()
        # The `or ((_CANCEL, "No projects available"),)` this replaces was an empty state
        # written before there was a mechanism for one, and it had the defect that shape
        # invites: the row was selectable, and `choose` has no `_CANCEL` branch, so pressing
        # enter on it fell through to `project is None` and announced "That project is no
        # longer available. Refresh and try again." — telling the owner a project had
        # vanished when the truth is that none has ever been there.
        self.show_choices(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}")
                for project in self.tui.catalogue
            ),
            trailing=((_BACK, "Back"),) if not self.tui.catalogue else (),
        )

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
            return
        project = next((item for item in self.tui.catalogue if item.opaque_id == key), None)
        if project is None:
            # Stay in the resume flow, as the launch picker does for the same failure,
            # rather than dropping the owner into a different wizard with no explanation.
            self.announce(
                "That project is no longer available. Refresh and try again.", severity="warning"
            )
            return
        conversations = self.services.conversations
        if conversations is None:
            return
        # Guarded across the read for the reason the hand-rolled flow established: a second
        # entry point firing mid-navigation used to reset the chosen project, after which
        # selecting a profile silently did nothing and only Escape recovered.
        async with self.holding_the_guard():
            try:
                capabilities = await conversations.capabilities()
            except Exception as error:
                _LOG.exception("resume capabilities failed")
                self.set_status(
                    "Resume is unavailable on this host. Press escape to return to the "
                    "project list.",
                    severity="error",
                )
                self.announce(f"Resume is unavailable: {error}")
                self.show_choices(((_BACK, "Back"),))
                return
            capable = tuple(
                capability
                for capability in capabilities
                if capability.catalogue_available and capability.selected_resume_available
            )
            # Inside the guard, not after it. `push_screen` yields while the new screen
            # mounts, so clearing first leaves a window in which a second of this app's
            # bindings pops the screen being mounted and the fetched capabilities are
            # discarded with no error — the same "a second entry point mid-navigation" class
            # the guard exists for, just failing silently instead of stranding. What the
            # guard does *not* cover is written down once, on `ChoiceScreen.advance_to`.
            await self.advance_to(ResumeProfilesScreen(project, capable))


class ResumeProfilesScreen(ChoiceScreen):
    """Only the agents that report themselves resume-capable on this host (DEC-002)."""

    #: **Not** "the same curated list as the launch wizard's", which is what this said at
    #: first writing and is the reason it was wrongly declared `NEVER_EMPTY`. The launch
    #: wizard renders all five closed profiles, greyed with a reason, so its list is fixed by
    #: construction; this one is filtered to the profiles that answered *yes* to a live
    #: capability probe, and DEC-002 is precisely the decision that the answer is asked rather
    #: than tabled — so "none of them can resume today" is an ordinary state, not a broken host.
    empty_state = "No agent on this host can resume a saved conversation."

    position = "RESUME_PROFILES"

    #: Its `on_reveal` already re-reads the profile capabilities from the provider on every
    #: back path, so there was something to
    #: re-read here all along. `can_refresh` was first set from "does this screen own a
    #: catalogue-style read" rather than from "is there anything here that goes stale", which
    #: is the question the footer is actually answering. Found by the Stage 1 gate evaluator.
    can_refresh = True

    async def refresh_contents(self) -> None:
        """Ctrl+R does here what coming back to this position does: read it again."""
        await self.on_reveal()

    def __init__(
        self, project: CatalogProject, capable: tuple[ProfileResumeCapability, ...]
    ) -> None:
        super().__init__()
        self.project = project
        self.capable = capable

    @property
    def crumb(self) -> str:
        """The project chosen a screen ago, as the launch wizard's agent step also names it."""
        return f"{self.project.area}/{self.project.name}"

    async def on_reveal(self) -> None:
        """Re-ask which agents can resume, because the answer can move while the owner is away.

        The chain this replaces re-ran the whole capability read on the way back from the
        conversation list, so a provider that stopped being resume-capable meanwhile was not
        still offered. A bare pop would have returned the owner to the answer from before
        they left — the same staleness `SessionsScreen` and `SessionDetailScreen` re-read to
        avoid, and it is only listed separately here because this flow was extracted a task
        after that precedent was set and did not inherit it.
        """
        conversations = self.services.conversations
        if conversations is None:
            return
        try:
            capabilities = await conversations.capabilities()
        except Exception as error:
            _LOG.exception("resume capabilities failed")
            # States the failure rather than pointing at the exit, and says so at error
            # severity — the same correction `app.report_store_failure` records: once the
            # toast has gone, a status that only offers escape leaves a failed read
            # indistinguishable from an ordinary empty list. Not the sibling's "return to
            # the project list": escape from here goes back one step, to the project
            # choice, so that wording would trade one wrong claim for another.
            self.set_status(
                "Resume is unavailable on this host. Press escape to go back.", severity="error"
            )
            self.announce(f"Resume is unavailable: {error}")
            self.show_choices(((_BACK, "Back"),))
            return
        self.capable = tuple(
            capability
            for capability in capabilities
            if capability.catalogue_available and capability.selected_resume_available
        )
        await self.populate()

    async def populate(self) -> None:
        self.hide_entry()
        if not self.capable:
            self.set_status("No agent on this host can resume a saved conversation.")
            self.show_choices((), trailing=((_BACK, "Back"),))
            return
        self.set_status("Choose the agent whose conversation you want to resume.")
        self.show_choices(
            tuple((str(item.profile_id), str(item.profile_id)) for item in self.capable)
            + ((_BACK, "Back"),)
        )

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
            return
        if not any(profile.profile_id == key for profile in self.services.profiles):
            # Defence in depth, matching the launch picker: the rows here are already
            # filtered to resume-capable profiles, so a key naming another one is stale.
            self.announce("That agent is not available on this host.", severity="warning")
            return
        async with self.holding_the_guard():
            page = await fetch_page(self, self.project, key, 1)
            if page is None:
                return
            # Held across the push for the reason given on `ResumeProjectsScreen.choose`.
            await self.advance_to(ResumeConversationsScreen(self.project, key, page))


class ResumeConversationsScreen(ChoiceScreen):
    """One bounded page of safe metadata; provider IDs never leave the server."""

    empty_state = "No saved conversations for this agent and project."

    position = "RESUME_CONVERSATIONS"

    #: Its `on_reveal` already re-reads the current page of conversations on every
    #: back path, so there was something to
    #: re-read here all along. `can_refresh` was first set from "does this screen own a
    #: catalogue-style read" rather than from "is there anything here that goes stale", which
    #: is the question the footer is actually answering. Found by the Stage 1 gate evaluator.
    can_refresh = True

    async def refresh_contents(self) -> None:
        """Ctrl+R does here what coming back to this position does: read it again."""
        await self.on_reveal()

    def __init__(
        self, project: CatalogProject, profile: str, page: ConversationCataloguePage
    ) -> None:
        super().__init__()
        self.project = project
        self.profile = profile
        # The paging state the app used to carry as `_resume_page` / `_resume_page_count`.
        # Local to the one screen that pages, so nothing else can move it under this screen.
        self.page = page

    @property
    def crumb(self) -> str:
        """The agent chosen a screen ago; the project is already the crumb before this one."""
        return self.profile

    async def on_reveal(self) -> None:
        """Re-read this page on the way back from the confirmation, for the same reason."""
        page = await fetch_page(self, self.project, self.profile, self.page.page)
        if page is None:
            return
        self.page = page
        self.render_page()

    async def populate(self) -> None:
        self.hide_entry()
        self.render_page()

    def render_page(self) -> None:
        page = self.page
        if page.unavailable_reason is not None:
            self.set_status(
                f"Conversations are unavailable: {page.unavailable_reason}", severity="warning"
            )
            self.show_choices(((_BACK, "Back"),))
            return
        # Filtered by the shared policy, which this surface did not consult at all until
        # BL-004 — it rendered whatever the catalogue returned. The bot had the rule twice and
        # this had it nowhere, and nothing had gone wrong only because `ConversationState` has
        # one member. `resume_available` is now the single authority, beside
        # `ConversationService` as `remote_control_available` sits beside `available_actions`.
        #
        # **Filtered before the empty check, not after it**, and the ordering is the whole of
        # the fix the Stage 3 gate asked for. The check below used to read
        # `page.conversations` — the *unfiltered* page — so a page whose rows were all refused
        # fell through to the choose-a-conversation branch, where `entries` picks up a `Back`
        # row and can therefore never be empty. `show_choices` substitutes `empty_state` only
        # when the whole tuple is empty, so it never fired either: the owner got "Choose a
        # conversation. Page 1 of 1." above no conversations at all. The bot filters first and
        # then asks `if not buttons:`, so this was also the one place the two surfaces would
        # have disagreed — in the task whose subject is making them agree.
        offered = [
            (str(item.reference), conversation_row(item))
            for item in page.conversations
            if resume_available(item)
        ]
        if not offered:
            self.set_status("There are no saved conversations for that agent and project.")
            self.show_choices((), trailing=((_BACK, "Back"),))
            return
        entries = list(offered)
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
            await self.tui.go_back()
            return
        if key in {_NEXT, _PREVIOUS}:
            await self.turn_page(1 if key == _NEXT else -1)
            return
        # Guarded across the resolve *and* the push, matching `:113` and `:232` — the two
        # siblings of this fetch — for the reason given there. This one was the exception
        # (DEC-024), and nothing chose it: the flow was hand-rolled with the other two
        # guarded, and this fetch was extracted afterwards without inheriting it. The cost is
        # real and accepted: a second entry point does nothing for the duration of one more
        # await. `turn_page` below takes the guard for itself and releases before
        # `render_page`, which is deliberate and stays that way — it redraws this position
        # rather than pushing another.
        async with self.holding_the_guard():
            try:
                # The reference is only ever one this surface rendered from a server-issued
                # page; constructing it here re-validates its opaque shape, and resolution is
                # server-side, so a forged or stale value resolves to nothing rather than a
                # path.
                resolved = await conversations.resolve_for_resume(ConversationReference(key))
            except ValueError:
                self.announce("That conversation selection is not valid.", severity="warning")
                return
            except Exception as error:
                _LOG.exception("conversation resolve failed")
                self.announce(f"That conversation could not be resolved: {error}")
                return
            if resolved is None:
                self.announce("That conversation is no longer available.", severity="warning")
                return
            # Inside the guard, not after it, for the reason `ResumeProjectsScreen.choose`
            # gives: `push_screen` yields while the new screen mounts, so releasing first
            # leaves a window in which one of this app's bindings pops the screen being
            # mounted and the resolved conversation is discarded with no error at all.
            # `ChoiceScreen.advance_to` records what this narrows and what it does not —
            # Textual's own `ctrl+p` is a priority binding and gets through regardless.
            await self.advance_to(ResumeConfirmScreen(self.project, self.profile, resolved))

    async def turn_page(self, step: int) -> None:
        wanted = max(1, min(self.page.page + step, self.page.page_count))
        async with self.holding_the_guard():
            page = await fetch_page(self, self.project, self.profile, wanted)
        if page is None:
            return
        self.page = page
        self.render_page()


class ResumeConfirmScreen(ChoiceScreen):
    """The last position before a session is started, resting on Cancel.

    The abort entry is first, so it is what the cursor rests on and what a stray enter
    activates — the same mitigation every other confirmation in this surface carries.
    """

    #: Resume, Back and Cancel are written here.
    empty_state = NEVER_EMPTY

    position = "RESUME_CONFIRM"
    #: "Confirm", not the conversation. Everywhere else in this surface the breadcrumb is
    #: where the subject goes, and this is the one position where it must not be: the header
    #: elides a long trail (`HeaderTitle` is `text-overflow: ellipsis`) and it also carries the
    #: whole trail before it, so the description — echoed from the agent's own output, and so
    #: the value here most likely to be long — is what gets cut. The status line takes it
    #: because it is the wider region and wraps to two rows.
    #:
    #: **Wider, not unbounded**, and the second gate pass was right to say the first version of
    #: this comment implied otherwise. A description near its 120-character bound still elides
    #: here at a narrow terminal. That is a smaller cut in a better place, not a guarantee; the
    #: honest fix if it matters is a position that can wrap freely, which is the output pane.
    crumb = "Confirm"

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
            f"Start a new session continuing: {conversation_row(self.resolved.summary)}"
        )
        self.show_choices(((_CANCEL, "Cancel"), ("resume-confirm", "Resume it")))

    async def choose(self, key: str) -> None:
        if key != "resume-confirm":
            await self.tui.go_back()
            return
        # Re-asked at the act rather than trusted from the row that got the owner here. What it
        # catches is a `resolve_for_resume` that disagrees with the `catalogue` listing — the
        # two are independent reads of the provider, and only the first was ever filtered.
        #
        # **It does not catch staleness while the owner deliberates on this screen**, and an
        # earlier version of this comment claimed it did, by analogy to `RemoteAgentsTui.stop`.
        # That analogy is wrong: `stop` genuinely re-reads the record (`current_record`), while
        # `self.resolved` is a frozen snapshot taken when this screen was pushed, so a pure
        # function of it cannot see anything move. Covering that window would mean re-resolving
        # here, which is DEC-024's shape one hop further and is not what BL-004 asked for.
        #
        # The same claim also said the bot had always had this check. It had not — it checked
        # while *rendering* the review screen and then resumed unchecked, which the Stage 3
        # gate's adversarial pass found and which is now fixed in `_resume_reply`.
        if not resume_available(self.resolved.summary):
            self.announce("That conversation can no longer be resumed.", severity="warning")
            await self.tui.go_back()
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
        # The other member of the same class as the one above. `screen` here is either the
        # profile choice or the conversation list, both of them below the project list, so
        # the navigation phrase stays "go back" while the sentence gains what it was
        # missing: what failed, at a severity that renders differently from an empty page.
        screen.set_status(
            "The conversations could not be listed. Press escape to go back.", severity="error"
        )
        screen.announce(f"The conversations could not be listed: {error}")
        screen.show_choices(((_BACK, "Back"),))
        return None
