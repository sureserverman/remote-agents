"""Telegram project selection, launches and saved conversations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from html import escape

from remote_agents.adapters.telegram.callbacks import permission_entity, split_permission_entity
from remote_agents.adapters.telegram.navigation import _button_rows
from remote_agents.adapters.telegram.presenters import Button, RenderedMessage
from remote_agents.adapters.telegram.screen import ScreenActions
from remote_agents.adapters.telegram.screen import reply_arguments as _reply_arguments
from remote_agents.adapters.telegram.trust_notifications import TrustNotifier, render_trust_question
from remote_agents.application.commands import LaunchCommand, ResumeCommand
from remote_agents.application.conversations import ConversationCatalogueQuery, resume_available
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.permission_presets import PERMISSION_TITLE, permission_label
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject, paginate_catalogue
from remote_agents.application.resume_flow import (
    RESUME_PAGE_SIZE,
    RESUME_PANE_UNAVAILABLE_MESSAGE,
    resume_capable,
)
from remote_agents.application.session_actions import state_word
from remote_agents.application.session_folders import (
    FOLDER_UNAVAILABLE_SENTENCE,
    WORKTREE_REMOVED_SENTENCE,
)
from remote_agents.application.session_views import selectable_area
from remote_agents.domain.conversations import ConversationReference
from remote_agents.domain.models import ProfileId, ProjectId, SessionRecord, SessionState
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.domain.trust import answerable as trust_answerable
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ProjectPicker:
    """The callbacks and wording one project list needs, so both flows share a renderer.

    Launch and resume both begin by choosing a project from the same catalogue, and only
    the action each button carries differs. Resume had its own renderer that emitted one
    button per project for the whole catalogue with no paging and no search: 95 rows
    against launch's 14 on this host, and Telegram refuses a keyboard past 100 buttons, so
    it would have stopped rendering entirely a few projects later.
    """

    select: str
    page: str
    search: str
    title: str
    instruction: str
    creates_projects: bool = False
    """Whether this flow may offer Add Project beside Search.

    Launch only. You cannot resume a prior conversation in a project that does not exist
    yet, so offering it there would be a route to a guaranteed dead end -- and since both
    flows share one renderer, that is exactly the regression sharing invites.
    """


_PROJECT_PICKERS = {
    "launch": _ProjectPicker(
        select="launch.project",
        page="launch.page",
        search="launch.search",
        title="Projects",
        instruction="Select a project to launch.",
        creates_projects=True,
    ),
    "resume": _ProjectPicker(
        select="resume.project",
        page="resume.projects",
        search="resume.search",
        title="Resume",
        instruction="Select the project for the prior conversation.",
    ),
}


def _split_launch(value: str) -> tuple[str, str]:
    project_id, separator, profile_id = value.partition("|")
    if not separator:
        raise ValueError("launch callback is invalid")
    return project_id, profile_id


def _split_resume_page(value: str) -> tuple[str, str, int] | None:
    project_id, separator, remainder = value.partition("|")
    profile_id, separator2, page_value = remainder.partition("|")
    if not separator or not separator2:
        return None
    try:
        page = int(page_value)
    except ValueError:
        return None
    return (project_id, profile_id, page) if page > 0 else None


def _resume_button_text(description: str | None, updated_at: datetime) -> str:
    """Keep a resume title useful without overflowing the compact keyboard.

    "Owner-approved" was the old wording here and it implied a vetting step that does not
    exist: this is the owner's own last instruction to the agent, read back out of the
    provider's transcript and truncated to fit a button (BL-007). See
    `domain/conversations.py` `ConversationSummary` for what is and is not screened.

    The obvious word for that text is one `tests/architecture/check_telegram_actions.py`
    forbids anywhere in this package, comments included, because its presence here would
    otherwise mean this adapter had grown a way to *send* one. The check is a plain substring
    scan and cannot tell prose from a call, which is the right trade for a guard on the
    control surface -- so the wording works around it rather than the guard being narrowed.
    """
    prefix = description[:48].rstrip() if description else "Resumable"
    return f"{prefix} · {updated_at:%Y-%m-%d %H:%M UTC}"


def _with_remote_control(remote_control: bool) -> str:
    """Name the flag the launch carried, and say nothing at all when it carried none.

    Only an affirmative has a phrase. There is no "without Remote Control" counterpart,
    because the absence covers three different situations the owner already understands --
    the row reads *off*, the row reads *Claude's default*, or this agent has no such setting
    at all -- and one sentence standing for all three would be read as a report about the
    first.

    Driven by what `SessionService.launch` actually passed, never by re-reading the setting
    here: the owner can edit the file between the launch and the reply, and a reply that
    disagreed with the launch is the disagreement class this stage exists to remove.
    """
    return " · with Remote Control" if remote_control else ""


@dataclass(kw_only=True)
class LaunchResumeScreens(ScreenActions):
    _project_views: dict[str, tuple[CatalogProject, ...]]
    get_catalogue: Callable[[], tuple[CatalogProject, ...]]
    profiles: tuple[ProfileAvailability, ...]
    project_page_size: int
    refresh_catalogue: Callable[[], Awaitable[None]]
    trust_dialogs: Mapping[str, object]
    trust_notifier: TrustNotifier | None
    _provider_name: Callable[[str], str]
    permission_question: Callable[[str, str], Awaitable[RenderedMessage]]

    async def _launch_reply(
        self, entity_id: str, token: str, message_id: int, *, mode: str | None = None
    ) -> dict[str, object]:
        """Launch the chosen agent; `mode` is None for the agent press, which first asks for a
        mode when the project is contended (BL-116), else the mode that press chose."""
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Launching is unavailable."))
        try:
            launch_entity, permissions = split_permission_entity(entity_id)
            project_id, profile_id = _split_launch(launch_entity)
        except ValueError:
            return _reply_arguments(self._message("That launch permission selection is invalid."))
        # Re-derived before the claim, not after. The confirmation screen checked both when it
        # drew the button, and a button now outlives the screen that drew it by any amount of
        # time -- so claiming first would burn the one-shot on a project that has since left
        # the catalogue and leave a FAILED row behind with no way to retry.
        if not any(project.opaque_id == project_id for project in self.get_catalogue()):
            return _reply_arguments(self._message("The project is no longer available."))
        if not any(
            profile.profile_id == profile_id and profile.available for profile in self.profiles
        ):
            return _reply_arguments(self._message("That agent is unavailable."))
        if mode is None and await self.backend.sessions.worktree_offered(ProjectId(project_id)):
            # Asked, not launched: the agent press spends nothing, and each mode button is a
            # fresh one-shot for the message that carries it (DEC-011).
            return _reply_arguments(self._launch_mode_question(entity_id))
        settings = self.backend.permission_presets
        if permissions is not None and (
            settings is None or permissions not in settings.profiles.get(ProfileId(profile_id), ())
        ):
            return _reply_arguments(
                self._message("Launch permissions are unavailable for this agent.")
            )
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            outcome = await self.backend.sessions.launch(
                LaunchCommand(
                    ProjectId(project_id),
                    ProfileId(profile_id),
                    token,
                    # No label at launch: choosing the agent starts the session, and naming it
                    # is a later, optional act from the session's own menu (Task 2.3).
                    None,
                    mode="worktree" if mode == "worktree" else "regular",
                    permissions=permissions,
                )
            )
        except CodexRemoteControlBusy:
            # Refused before anything was recorded (BL-037), so pressing again is a real
            # attempt -- but not this button, whose token is spent. A fresh one, for the same
            # project and agent.
            return _reply_arguments(
                self._message(
                    "Codex Remote Control is changing on this machine — launch again in a "
                    "moment. Nothing was started.",
                    (
                        (
                            Button(
                                "Launch again",
                                self._callback("launch.profile", entity_id, mutation=True)
                                if mode is None
                                else self._callback(
                                    "launch.mode",
                                    f"{entity_id}|{'w' if mode == 'worktree' else 'r'}",
                                    mutation=True,
                                ),
                            ),
                        ),
                    ),
                )
            )
        if outcome is None:
            return _reply_arguments(self._message("Session launch requested."))
        record = outcome.record
        if record.state is SessionState.FAILED and outcome.worktree_failed is not None:
            # Refused before any pane existed, as below; git's own reason says why.
            return _reply_arguments(
                self._message(
                    f"<b>Session did not start</b>\n{escape(record.display.rendered)}\n"
                    f"Its worktree could not be made: {escape(outcome.worktree_failed)}",
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                    ),
                )
            )
        if record.state is SessionState.FAILED and outcome.folder_unavailable:
            return _reply_arguments(
                self._message(
                    f"<b>Session did not start</b>\n{escape(record.display.rendered)}\n"
                    f"{FOLDER_UNAVAILABLE_SENTENCE}",
                    (
                        (
                            Button(
                                "Details", self._callback("session.detail", str(record.session_id))
                            ),
                        ),
                    ),
                )
            )
        if record.state is SessionState.FAILED and outcome.worktree_removed:
            # Refused before any pane existed, so "its pane may still exist" would send the
            # owner looking for one (BL-116).
            return _reply_arguments(
                self._message(
                    f"<b>Session did not start</b>\n{escape(record.display.rendered)}\n"
                    f"{WORKTREE_REMOVED_SENTENCE}",
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                    ),
                )
            )
        if record.state is SessionState.FAILED:
            return _reply_arguments(
                self._message(
                    f"<b>Session did not become ready</b>\n{escape(record.display.rendered)}\n"
                    "Its pane may still exist. Check this host, then open Sessions to "
                    "recheck.",
                    # Details only. "Sessions" and "Launch another" were the ways on before
                    # a permanent way on existed, and both now name a destination the bar
                    # carries on the next row -- "Launch another" beside a *marked* Launch.
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                    ),
                )
            )
        if record.state is SessionState.UNTRUSTED:
            return _reply_arguments(self._trust_question(record))
        return _reply_arguments(
            self._message(
                f"<b>Session created</b>{_with_remote_control(outcome.remote_control)}\n"
                f"{escape(record.display.rendered)}\nState: {record.state}",
                ((Button("Inspect", self._callback("session.detail", str(record.session_id))),),),
            )
        )

    def _launch_mode_question(self, entity_id: str) -> RenderedMessage:
        """Another session is live on this project: work beside it, or in a worktree of its
        own (BL-116). Only ever drawn on contention; a lone launch is one press."""
        return self._message(
            "<b>Another session is working in this project</b>\n"
            "Share its folder, or give this one its own worktree and branch?",
            (
                (
                    Button(
                        "Shared folder",
                        self._callback("launch.mode", f"{entity_id}|r", mutation=True),
                    ),
                    Button(
                        "Own worktree",
                        self._callback("launch.mode", f"{entity_id}|w", mutation=True),
                    ),
                ),
            ),
        )

    def _trust_question(self, record: SessionRecord) -> RenderedMessage:
        """The launch reply for a session that came up on its agent's folder-trust dialog.

        **The launch did not fail, and the reply must not read like it did.** What the owner
        got before this state existed was "Session did not become ready" after the whole
        startup budget -- true, and useless (DEC-016). The agent is up and one answer away.

        Both answers, on rows of their own, offered on the same conditions the detail screen
        uses: a profile whose dialog this project cannot read gets only the *no*.
        """
        session_value = str(record.session_id)
        answerable = trust_answerable(record.profile_id, self.trust_dialogs)
        # Through the shared renderer, not beside it. The notification sent on its own says
        # same thing about the same session, and two wordings for one question is one wording
        # plus a future divergence -- which is the shape DEC-043 keeps out of this adapter.
        #
        # `render_trust_question` rather than `self._message`: this reply is a question the
        # owner answers, and the two answers are the whole of it. It is the same barless
        # construction the activity notification uses (DEC-032).
        if self.trust_notifier is not None:
            # The reply *is* the question, so the pass that would otherwise send one five
            # seconds from now must stand down for this session. Told rather than inferred:
            # nothing in the record distinguishes "asked on a screen" from "not yet asked".
            self.trust_notifier.note_asked_on_screen(record.session_id)
        question = render_trust_question(
            record,
            answerable=answerable,
            trust=(
                self._callback("session.trust", session_value, mutation=True)
                if answerable
                else None
            ),
            decline=self._callback("session.decline", session_value, mutation=True),
        )
        # Re-wrapped through `_message` so the bar comes back, and that is not a
        # contradiction of DEC-032 -- it is DEC-032. A notification is barless because it is a
        # *message*, outliving the screen it was sent from; this is a **screen the owner is
        # standing on** immediately after a launch, and every other launch reply carries the
        # bar. What the two share is the wording and the shape of the answers, which is what
        # the renderer is for; where the owner can go next is a property of the surface they
        # are on.
        return self._message(question.text, question.keyboard)

    async def _resume_reply(
        self, reference_value: str, token: str, message_id: int
    ) -> dict[str, object]:
        if self.backend.sessions is None or self.backend.conversations is None:
            return _reply_arguments(self._message("Resuming is unavailable."))
        # Everything re-derivable is re-derived **before** the claim, which is the ordering
        # `_launch_reply` records and the ordering this path did not have: claiming first
        # burns the one-shot on a conversation or a project that has since gone, and leaves
        # the owner a button that answers "already run" for something that never ran.
        try:
            reference_value, permissions = split_permission_entity(reference_value)
        except ValueError:
            return _reply_arguments(self._message("That resume permission selection is invalid."))
        resolved = await self._resolve_resume(reference_value)
        if resolved is None or resolved.summary.project_id is None:
            return _reply_arguments(self._message("That conversation is no longer available."))
        # The check the review screen used to carry. It moved to the act rather than leaving
        # with the screen -- a rendered row outlives the catalogue it was drawn from.
        if not any(
            project.opaque_id == str(resolved.summary.project_id)
            for project in self.get_catalogue()
        ):
            return _reply_arguments(self._message("The project is no longer available."))
        # Re-checked at the *act*, not only where the row was drawn. This is the mitigation
        # `StopController.execute` already applies to every stop -- the rendered row is
        # re-tested against the shared policy before the command goes out -- and the resume
        # path did not have it on either surface. It matters more now that the row *is* the
        # act: there is no second screen between the catalogue read that drew it and the
        # resume it performs.
        if not resume_available(resolved.summary):
            return _reply_arguments(self._message("That conversation cannot be resumed safely."))
        # The last re-derivation `_launch_reply` did and this did not. A profile can stop
        # being available between the row being drawn and the row being pressed, and the
        # rendered row outlives the capability probe that drew it.
        if not any(
            profile.profile_id == str(resolved.summary.profile_id) and profile.available
            for profile in self.profiles
        ):
            return _reply_arguments(self._message("That agent is unavailable."))
        settings = self.backend.permission_presets
        if permissions is not None and (
            settings is None
            or permissions not in settings.profiles.get(resolved.summary.profile_id, ())
        ):
            return _reply_arguments(
                self._message("Launch permissions are unavailable for this agent.")
            )
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            outcome = await self.backend.sessions.resume(
                ResumeCommand(
                    resolved.summary.project_id,
                    resolved.summary.profile_id,
                    resolved,
                    token,
                    permissions=permissions,
                )
            )
        except CodexRemoteControlBusy:
            # Refused before anything was recorded (BL-037); the conversation is untouched.
            return _reply_arguments(
                self._message(
                    "Codex Remote Control is changing on this machine — resume again in a "
                    "moment. Nothing was started."
                )
            )
        record = outcome.record
        if outcome.pane_unavailable:
            return _reply_arguments(self._message(escape(RESUME_PANE_UNAVAILABLE_MESSAGE)))
        if outcome.created and record.state is SessionState.FAILED and outcome.folder_unavailable:
            return _reply_arguments(
                self._message(f"<b>Resume did not start</b>\n{FOLDER_UNAVAILABLE_SENTENCE}")
            )
        if outcome.created and record.state is SessionState.FAILED and outcome.worktree_removed:
            # Refused before any pane existed: there is nothing to attend to locally (BL-116).
            return _reply_arguments(
                self._message(f"<b>Resume did not start</b>\n{WORKTREE_REMOVED_SENTENCE}")
            )
        if outcome.created and record.state is SessionState.FAILED:
            # A resume this press really did create, whose pane did not come up. Stage 3's
            # deliberate decision (`bb23946`, repaired in `4f2cf88`) that FAILED keeps its own
            # message about resolving trust locally — and the message is about *this press's*
            # resume, so it is owed the `created` guard that the rest of this method now has.
            #
            # It was FAILED-first and unguarded until close-out, which left the same
            # over-claim the `created` branch below exists to remove, just narrower: pressing
            # a conversation bound to an *existing* failed session said "Resume did not become
            # ready" — a sentence about an attempt this press never made — and withheld the
            # attachment explanation its sibling gives. Half-fixing an over-claim is how the
            # remaining half stops looking like one.
            return _reply_arguments(
                self._message(
                    "<b>Resume did not become ready</b>\nOpen Sessions after local attention."
                )
            )
        if not outcome.created:
            # Nothing was started: the conversation was already bound and the service handed
            # back the existing record. This asks what the service **did**, where it used to
            # ask what state the record was in — and no question about the state can answer
            # it, because an already-bound RUNNING session and a resume that has just come up
            # are indistinguishable. That is how "Session resumed" came to be printed over a
            # live session this press had not touched, contradicting both the README and step
            # 12 of the acceptance checklist. The states that used to reach here (ENDED,
            # PRESERVED, STOP_REQUESTED, ORPHANED) still do, and RUNNING and STARTING now do
            # too, which is the whole of the repair.
            return _reply_arguments(
                self._message(
                    f"<b>Not resumed</b>\n{escape(record.display.rendered)}\n"
                    f"This conversation is attached to that session, which is now "
                    f"{state_word(record.state, record.orphan_provenance)}. It becomes "
                    "resumable again once that session has ended — open it to see what it "
                    "offers.",
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                    ),
                )
            )
        return _reply_arguments(
            self._message(
                f"<b>Session resumed</b>\n{escape(record.display.rendered)}\nState: {record.state}",
                ((Button("Inspect", self._callback("session.detail", str(record.session_id))),),),
            )
        )

    async def _project_areas_reply(self) -> RenderedMessage:
        """Offer only the server-enumerated areas; a typed area never reaches the filesystem."""
        if self.backend.projects is None:
            return self._message("Adding a project is unavailable.")
        areas = tuple(
            area
            for area in await asyncio.to_thread(self.backend.projects.available_areas)
            if selectable_area(area)
        )
        if not areas:
            return self._message("No area is available for a new project.")
        return self._message(
            "<b>Add project</b>\nSelect the area for the new project.",
            _button_rows(
                tuple(Button(area, self._callback("project.area", area)) for area in areas)
            )
            # Cancel returns to the launch list, which is the screen that offered Add
            # Project. It used to mint `nav.home`; Home was a defensible cancel target while
            # it was the root, and the sessions list -- which is what that action now answers
            # -- is the parent of nothing in this flow.
            + ((Button("Cancel", self._callback("launch.open", "projects")),),),
        )

    def _project_review_reply(self, identity: ProjectIdentity) -> RenderedMessage:
        return self._message(
            f"<b>Review new project</b>\nArea: {escape(identity.area)}\n"
            f"Name: {escape(identity.name)}",
            (
                (
                    Button(
                        "Create",
                        self._callback(
                            "project.confirm",
                            f"{identity.area}|{identity.name}",
                            mutation=True,
                        ),
                    ),
                ),
                (Button("Back", self._callback("project.open", "areas")),),
                (Button("Cancel", self._callback("launch.open", "projects")),),
            ),
        )

    async def _project_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Create at most once per confirmation, then re-read the catalogue off the loop."""
        if self.backend.projects is None:
            return _reply_arguments(self._message("Adding a project is unavailable."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        area, separator, name = entity_id.partition("|")
        if not separator:
            return _reply_arguments(self._message("That action has already run."))
        try:
            created = await asyncio.to_thread(
                self.backend.projects.create, CreateProjectCommand(area, name)
            )
        except ProjectCreationError as error:
            return _reply_arguments(
                self._message(f"<b>Project not created</b>\n{escape(str(error))}")
            )
        except Exception:
            _LOG.exception("project creation failed outside the application's error contract")
            return _reply_arguments(self._message("<b>Project not created</b>\nCheck this host."))
        await self.refresh_catalogue()
        return _reply_arguments(
            self._message(
                f"<b>Project created</b>\n{escape(str(created.identity))}",
            )
        )

    def _projects_reply(
        self,
        projects: tuple[CatalogProject, ...],
        *,
        view_id: str,
        page: int = 1,
        flow: str = "launch",
    ) -> RenderedMessage:
        """Render one page of the project catalogue for whichever flow asked for it.

        The stored view is keyed by flow as well as view id, so a search inside Resume
        cannot be paged into by Launch and vice versa.
        """
        picker = _PROJECT_PICKERS[flow]
        self._project_views[f"{flow}:{view_id}"] = projects
        try:
            rendered = paginate_catalogue(projects, page, self.project_page_size)
        except ValueError:
            return self._message("That project list is no longer open. Open Launch again.")
        buttons = [
            (
                Button(
                    project.name,
                    self._callback(picker.select, project.opaque_id),
                ),
            )
            for project in rendered.projects
        ]
        navigation = []
        if rendered.page > 1:
            navigation.append(
                Button("Previous", self._callback(picker.page, f"{view_id}|{rendered.page - 1}"))
            )
        if rendered.page < rendered.page_count:
            navigation.append(
                Button("Next", self._callback(picker.page, f"{view_id}|{rendered.page + 1}"))
            )
        if navigation:
            buttons.append(tuple(navigation))
        # Beside Search, because they answer the same moment: the project you wanted is not
        # on this screen. Add Project used to live on Home, one level up from the only screen
        # that can tell you it is missing.
        finders = [Button("Search", self._callback(picker.search, "search"))]
        if picker.creates_projects and self.backend.projects is not None:
            finders.append(Button("Add Project", self._callback("project.open", "areas")))
        buttons.append(tuple(finders))
        # No `back`. This screen had exactly one parent when Home was the only way to reach
        # it; the bar reaches it in one press from anywhere, so a Back pointing at Home now
        # lands the owner somewhere they were never standing — which is the one thing Back
        # must not do. `_message`'s contract is "pass it wherever there is a real parent",
        # and a screen reachable from everywhere has none. The bar is the way out.
        return self._message(
            f"<b>{picker.title} {rendered.page}/{rendered.page_count}</b>\n{picker.instruction}",
            tuple(buttons),
        )

    def _project_page_reply(self, entity_id: str, flow: str = "launch") -> RenderedMessage:
        view_id, separator, page_value = entity_id.partition("|")
        if not separator or view_id not in {"all", "search"}:
            return self._message("That project list is no longer open. Open Launch again.")
        try:
            page = int(page_value)
        except ValueError:
            return self._message("That project list is no longer open. Open Launch again.")
        projects = self._project_views.get(f"{flow}:{view_id}")
        if projects is None and view_id == "all":
            # The stored view is process-local, so a restart empties it while the button that
            # reads it now survives. "all" is reconstructible -- it is the current catalogue --
            # so paging re-renders rather than refusing, and only a search, whose query nobody
            # kept, has to send the owner back.
            projects = self.get_catalogue()
        if projects is None:
            return self._message("That search is no longer open. Search again.")
        return self._projects_reply(projects, view_id=view_id, page=page, flow=flow)

    def _resume_projects_reply(self) -> RenderedMessage:
        return self._projects_reply(self.get_catalogue(), view_id="all", flow="resume")

    async def _resume_profiles_reply(self, project_id: str) -> RenderedMessage:
        if not any(project.opaque_id == project_id for project in self.get_catalogue()):
            return self._message("The project is no longer available.")
        if self.backend.conversations is None:
            return self._message("Resume is unavailable.")
        capabilities = {
            str(item.profile_id): item for item in await self.backend.conversations.capabilities()
        }
        buttons = []
        unavailable = []
        for profile in self.profiles:
            capability = capabilities.get(profile.profile_id)
            # `profile.available` and the `None` capability stay the bot's own: the local
            # surface renders no row at all for an agent it cannot offer, so it never asks
            # either question. `resume_capable` is the part both surfaces were answering
            # separately, and the `reason` composed in the `else` below is what the bot does
            # with the answer rather than part of it.
            if profile.available and capability is not None and resume_capable(capability):
                buttons.append(
                    Button(
                        self._provider_name(profile.profile_id),
                        self._callback("resume.profile", f"{project_id}|{profile.profile_id}|1"),
                    )
                )
            else:
                # `any_reason` is the blocking reason when there is one and the probe note
                # otherwise -- this branch is reached with `available` true whenever the
                # catalogue is not resume-capable, and it has always shown the note there.
                reason = profile.any_reason or (
                    capability.reason if capability is not None else "catalogue_unavailable"
                )
                unavailable.append(f"{self._provider_name(profile.profile_id)} ({reason})")
        text = "<b>Select a resumable agent</b>"
        if unavailable:
            text += "\nUnavailable: " + escape(", ".join(unavailable))
        return self._message(
            text,
            _button_rows(tuple(buttons))
            + ((Button("Back", self._callback("resume.open", "projects")),),),
        )

    async def _resume_catalogue_reply(self, entity_id: str) -> RenderedMessage:
        try:
            entity, permissions = split_permission_entity(entity_id)
        except ValueError:
            return self._message("That permission choice is no longer available.")
        parsed = _split_resume_page(entity)
        if parsed is None or self.backend.conversations is None:
            return self._message("That conversation list is no longer open.")
        project_id, profile_id, page = parsed
        settings = self.backend.permission_presets
        if permissions is not None and (
            settings is None or permissions not in settings.profiles.get(ProfileId(profile_id), ())
        ):
            return self._message("Launch permissions are unavailable for this agent.")
        if not any(project.opaque_id == project_id for project in self.get_catalogue()):
            return self._message("The project is no longer available.")
        try:
            result = await self.backend.conversations.catalogue(
                ConversationCatalogueQuery(
                    page, RESUME_PAGE_SIZE, ProfileId(profile_id), ProjectId(project_id)
                )
            )
        except ValueError:
            return self._message("That conversation list is no longer open.")
        if result.unavailable_reason is not None:
            return self._message(f"Resume is unavailable ({escape(result.unavailable_reason)}).")
        buttons = tuple(
            (
                Button(
                    _resume_button_text(summary.description, summary.updated_at),
                    # The act, not a step toward it. Launch stopped asking for a review when
                    # choosing the agent became the act; choosing a *named conversation* is a
                    # more specific choice than choosing an agent, so resume was charging an
                    # extra press for less ambiguity.
                    #
                    # An earlier version of this comment claimed DEC-008 made the single
                    # press safe. It does not, and the error mattered: DEC-008 is about
                    # *repeats* — `claim_mutation` admits one caller per token, which makes
                    # the **second** press safe and says nothing about the first, unintended
                    # one. What the first press costs here is not what it costs on launch:
                    # `SessionService._resume_locked` binds a conversation to the session it
                    # creates. Migration 8 is what stops that being *permanent* — the unique
                    # index is partial on `state <> 'ended'`, so the conversation binds again
                    # once its session has ended. An unwanted resume therefore costs a
                    # session to stop, not a conversation forever.
                    self._callback(
                        "resume.confirm",
                        permission_entity(str(summary.reference), permissions),
                        mutation=True,
                    ),
                ),
            )
            for summary in result.conversations
            if resume_available(summary)
        )
        navigation = []
        if result.page > 1:
            navigation.append(
                Button(
                    "Previous",
                    self._callback(
                        "resume.page",
                        permission_entity(
                            f"{project_id}|{profile_id}|{result.page - 1}", permissions
                        ),
                    ),
                )
            )
        if result.page < result.page_count:
            navigation.append(
                Button(
                    "Next",
                    self._callback(
                        "resume.page",
                        permission_entity(
                            f"{project_id}|{profile_id}|{result.page + 1}", permissions
                        ),
                    ),
                )
            )
        # Back belongs in the navigation rows `_message` appends, like every other screen,
        # rather than as a body button — and the empty case needs it most, since it used to
        # offer nothing but a row restating that there was nothing.
        back = self._callback("resume.project", project_id)
        if not buttons:
            return self._message(
                "<b>Prior conversations</b>\nThis agent has no resumable conversation "
                "for this project.",
                back=back,
            )
        rows = list(buttons)
        settings = self.backend.permission_presets
        if settings is not None and ProfileId(profile_id) in settings.profiles:
            rows.append(
                (
                    Button(
                        f"{PERMISSION_TITLE}: {permission_label(permissions)}",
                        self._callback("resume.permissions", entity_id),
                    ),
                )
            )
        if navigation:
            rows.append(tuple(navigation))
        return self._message(
            f"<b>Prior conversations {result.page}/{result.page_count}</b>\n"
            "Select a resumable conversation.",
            tuple(rows),
            back=back,
        )

    async def _resolve_resume(self, reference_value: str):
        if self.backend.conversations is None:
            return None
        try:
            reference = ConversationReference(reference_value)
        except ValueError:
            return None
        return await self.backend.conversations.resolve_for_resume(reference)

    async def _profiles_reply(self, project_id: str) -> RenderedMessage:
        try:
            project_id, permissions = split_permission_entity(project_id)
        except ValueError:
            return self._message("That permission choice is no longer available.")
        if not any(project.opaque_id == project_id for project in self.get_catalogue()):
            return self._message("The project is no longer available.")
        settings = self.backend.permission_presets
        if permissions is not None and (
            settings is None
            or not any(
                item.available
                and permissions in settings.profiles.get(ProfileId(item.profile_id), ())
                for item in self.profiles
            )
        ):
            return self._message("Launch permissions are unavailable for this agent.")
        buttons = tuple(
            Button(
                self._provider_name(profile.profile_id),
                # The mutation is claimed here rather than on a review screen that no longer
                # exists. DEC-008 makes the *repeat* safe — a second press of the same button
                # is dropped by the one-shot claim, never serviced into a second session, and
                # Sub-plan 1 made that claim durable rather than process-local. It says
                # nothing about the first, unintended press; what makes that acceptable here
                # is specific to launch, namely that an unwanted launch creates a disposable
                # session and costs a stop. The resume path thirty lines below carries the
                # same correction, because assuming DEC-008 covered the first press is exactly
                # the error that let a one-press resume ship and become a gate escalation.
                self._callback(
                    "launch.profile",
                    permission_entity(
                        f"{project_id}|{profile.profile_id}",
                        permissions
                        if settings is not None
                        and ProfileId(profile.profile_id) in settings.profiles
                        else None,
                    ),
                    mutation=True,
                ),
            )
            for profile in self.profiles
            if profile.available
        )
        schedule_row: tuple[tuple[Button, ...], ...] = ()
        if self.backend.schedules is not None:
            # Beside launching, not instead of it: the same agents, started later (DEC-114).
            schedule_row = ((Button("⏰ Schedule", self._callback("sched.agents", project_id)),),)
        permission_row = ()
        if settings is not None and any(
            item.available and ProfileId(item.profile_id) in settings.profiles
            for item in self.profiles
        ):
            try:
                selected = permissions or await settings.read()
            except Exception as error:
                return self._message(f"Launch permissions could not be read: {escape(str(error))}")
            permission_row = (
                (
                    Button(
                        f"{PERMISSION_TITLE}: {permission_label(selected)}",
                        self._callback(
                            "launch.permissions", permission_entity(project_id, permissions)
                        ),
                    ),
                ),
            )
        return self._message(
            "<b>Select an agent</b>",
            _button_rows(buttons)
            + permission_row
            + schedule_row
            + ((Button("Back", self._callback("launch.open", "projects")),),),
        )

    async def reply_for(
        self, action: str, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        if action == "resume.confirm":
            return await self._resume_reply(entity_id, token, message_id)

        if action == "project.confirm":
            return await self._project_reply(entity_id, token, message_id)

        if action == "project.open":
            return _reply_arguments(await self._project_areas_reply())

        if action == "launch.open":
            await self.refresh_catalogue()
            return _reply_arguments(self._projects_reply(self.get_catalogue(), view_id="all"))

        if action == "launch.page":
            return _reply_arguments(self._project_page_reply(entity_id))

        if action == "launch.project":
            return _reply_arguments(await self._profiles_reply(entity_id))

        if action in {"launch.permissions", "resume.permissions"}:
            return _reply_arguments(await self.permission_question(action.split(".")[0], entity_id))

        if action in {"launch.permissions.set", "resume.permissions.set"}:
            try:
                split_permission_entity(entity_id)
            except ValueError:
                return _reply_arguments(
                    self._message("That permission choice is no longer available.")
                )
            return _reply_arguments(
                await self._profiles_reply(entity_id)
                if action.startswith("launch.")
                else await self._resume_catalogue_reply(entity_id)
            )

        if action == "launch.profile":
            return await self._launch_reply(entity_id, token, message_id)

        if action == "launch.mode":
            entity_id, _, mode = entity_id.rpartition("|")
            return await self._launch_reply(
                entity_id, token, message_id, mode="worktree" if mode == "w" else "regular"
            )

        if action == "resume.select":
            # Retired with the review screen, and kept rather than dropped for the reason
            # `nav.refresh` is kept: tokens live in SQLite and are valid for their message
            # rather than for a clock (DEC-011), so a conversation list drawn before this
            # deploy still has rows carrying it. Answering with what changed beats the
            # generic "no longer available", which reads as though the conversation went.
            return _reply_arguments(
                self._message(
                    "Resuming no longer has a review step — choosing a conversation starts "
                    "it. Open Resume and choose it again."
                )
            )

        if action == "resume.open":
            await self.refresh_catalogue()
            return _reply_arguments(self._resume_projects_reply())

        if action == "resume.projects":
            return _reply_arguments(self._project_page_reply(entity_id, flow="resume"))

        if action == "resume.project":
            return _reply_arguments(await self._resume_profiles_reply(entity_id))

        if action in {"resume.profile", "resume.page"}:
            return _reply_arguments(await self._resume_catalogue_reply(entity_id))

        return _reply_arguments(self._message("That action is no longer available."))
