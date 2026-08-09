"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar, cast

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.worker import WorkerCancelled, WorkerFailed

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.model import (
    _BACK,
    _CANCEL,
    _NEXT,
    _PREVIOUS,
    AttachRequest,
    LaunchSelection,
    age,
    conversation_row,
    label_or_error,
    selectable_area,
    session_row,
)
from remote_agents.adapters.tui.screens import (
    ALL_SCREENS,
    AreasScreen,
    LegacyScreen,
    ProjectsScreen,
    SessionDetailScreen,
    SessionsScreen,
)
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.conversations import ConversationCatalogueQuery
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import (
    ACTION_LABELS as _ACTION_LABELS,
)
from remote_agents.application.session_actions import (
    CLEANUP,
    FORCE,
    GRACEFUL,
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.domain.conversations import ConversationReference
from remote_agents.domain.models import ProfileId, ProjectId, SessionRecord, SessionState
from remote_agents.domain.remote_control import RemoteControlState

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")

# Re-exported so importers that predate the `model` split keep working, and because these
# are part of this module's published surface: tests and the composition root both take
# `AttachRequest` from here.
__all__ = [
    "ALL_SCREENS",
    "AttachRequest",
    "LaunchSelection",
    "ProfileChoice",
    "RemoteAgentsTui",
    "Step",
    "TuiContext",
    "age",
    "conversation_row",
    "label_or_error",
    "run_local_terminal",
    "selectable_area",
    "session_row",
]


class Step(StrEnum):
    """The wizard positions this stage has not extracted into screens yet.

    Shrinking, not growing. Ten of the sixteen members are now screens and are kept here only
    because the committed snapshot baselines still name them; what still *drives* anything is
    the four resume positions, hosted on `LegacyScreen`, and the two confirmations, which
    `SessionDetailScreen` repaints in place exactly as the surface does today. Task 2.3 takes
    the first group and Stage 3 takes the second, after which Task 2.4 deletes the enum.
    """

    PROJECTS = "projects"
    PROFILES = "profiles"
    LABEL = "label"
    REVIEW = "review"
    AREAS = "areas"
    NAME = "name"
    PROJECT_REVIEW = "project-review"
    SESSIONS = "sessions"
    SESSION_DETAIL = "session-detail"
    FORCE_CONFIRM = "force-confirm"
    REMOTE_CONTROL_CONFIRM = "remote-control-confirm"
    INSPECT = "inspect"
    RESUME_PROJECTS = "resume-projects"
    RESUME_PROFILES = "resume-profiles"
    RESUME_CONVERSATIONS = "resume-conversations"
    RESUME_CONFIRM = "resume-confirm"


_RESUME_PAGE_SIZE = 10


class RemoteAgentsTui(App[AttachRequest | None]):
    """Choose a project and an agent, launch it, and hand back an attach command."""

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #status { height: auto; padding: 0 1; }
    OptionList { height: 1fr; }
    #output { height: 1fr; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("ctrl+n", "add_project", "Add project"),
        Binding("ctrl+s", "sessions", "Sessions"),
        Binding("ctrl+o", "resume", "Resume"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, context: TuiContext) -> None:
        super().__init__()
        self._services = context
        self._catalogue = context.catalogue
        self.selection = LaunchSelection()
        self._step = Step.PROJECTS
        self._busy = False
        self._resume_project: CatalogProject | None = None
        self._resume_profile: str | None = None
        self._resume_page = 1
        self._resume_page_count = 1
        self._resume_choice: object | None = None

    def get_default_screen(self) -> Screen[None]:
        """The project list, installed as the bottom of the stack rather than pushed.

        `pop_screen` raises `ScreenStackError` on the last screen, so making the resting
        position the *default* screen is what turns "a back path must never empty the stack"
        from a rule every screen has to observe into something the stack cannot do.
        """
        return ProjectsScreen()

    # Shared state screens read ---------------------------------------------------

    @property
    def services(self) -> TuiContext:
        return self._services

    @property
    def catalogue(self) -> tuple[CatalogProject, ...]:
        return self._catalogue

    @property
    def busy(self) -> bool:
        """Whether an action is mid-flight and no other may start."""
        return self._busy

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    @property
    def step(self) -> Step:
        """Which position the remaining step machine believes it is on.

        Read by exactly two places now — `SessionDetailScreen`, for the two confirmations it
        still repaints in place, and the resume flow on `LegacyScreen`. Both go away by the
        end of Stage 3, and so does this.
        """
        return self._step

    @step.setter
    def step(self, step: Step) -> None:
        self._step = step

    @property
    def body(self) -> ChoiceScreen:
        """The active screen, typed as the body every position renders."""
        return cast(ChoiceScreen, self.screen)

    @property
    def detail_session(self) -> str | None:
        """The session the detail on screen was opened for, if a detail is on screen.

        This replaces the `_detail_id` field the app used to carry. Reading it off the screen
        rather than off the app is what makes a stale value impossible: the id and the screen
        rendering it are now the same object's state, so navigating away cannot leave one
        behind for a later action to pick up.
        """
        screen = self.screen
        return screen.session_value if isinstance(screen, SessionDetailScreen) else None

    def return_to_projects(self) -> None:
        """Unwind to the resting position, whatever the owner had pushed on top of it."""
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self._step = Step.PROJECTS
        screen = self.screen
        if isinstance(screen, ProjectsScreen):
            screen.render_projects()

    async def switch_flow(self, screen: Screen[None]) -> None:
        """Leave whatever flow is open and start another one from the resting position.

        The three global bindings — sessions, add project, resume — are jumps between flows
        rather than steps within one, and the chain this replaces implemented them by
        *replacing* the position outright. Unwinding first is the faithful translation:
        entering the sessions view from three levels into the launch wizard has always
        returned the owner to the project list on escape, and stacking instead would make the
        depth depend on where they happened to be.
        """
        while len(self.screen_stack) > 1:
            self.pop_screen()
        await self.push_screen(screen)

    # Rendering -------------------------------------------------------------------
    #
    # These delegate to the active screen. They are what remains of the app-owned rendering
    # the extracted screens took over, kept for the resume positions still hosted on
    # `LegacyScreen`; Task 2.4 deletes them with it.

    def _set_status(self, text: str) -> None:
        self.body.set_status(text)

    def _fill(
        self, entries: tuple[tuple[str, str], ...], *, focus: bool = True, highlight: int = 0
    ) -> None:
        self.body.show_choices(entries, focus=focus, highlight=highlight)

    def _hide_entry(self) -> None:
        self.body.hide_entry()

    async def in_thread(self, work: Callable[[], _T], *, group: str) -> _T:
        """Run one blocking call on a worker thread and return what it returned.

        The worker is owned by this app, so it is cancelled when the app goes away rather
        than being left to write into a torn-down screen. The `group` is a **label only**:
        `run_worker`'s `exclusive` defaults to `False` and is deliberately not passed here
        (DEC-008), so nothing supersedes anything — it exists to make a flow identifiable in
        worker introspection. The cancellation is of the *worker*, not of the OS thread: a
        blocking call already in progress runs to completion and only its result is dropped.

        `exit_on_error=False` because these calls read a development root and a registry on
        a host that may be misconfigured — that is an error each caller already reports and
        recovers from, not a reason to take the app down. `WorkerFailed` is unwrapped for
        the same reason: callers put the failure on screen, and "Worker raised exception:
        OSError(...)" is not what the owner needs to read.

        `WorkerCancelled` becomes `CancelledError` instead of being unwrapped. It means the
        app is shutting down, not that the read failed, and the callers' `except Exception`
        would otherwise catch it and try to render an error into a screen being torn down.
        `CancelledError` is a `BaseException`, so it passes through those handlers untouched
        — which is what the plain `await` these calls replaced already did.
        """
        worker = self.run_worker(work, thread=True, group=group, exit_on_error=False)
        try:
            return await worker.wait()
        except WorkerFailed as failure:
            raise failure.error from None
        except WorkerCancelled as cancelled:
            raise asyncio.CancelledError(str(cancelled)) from None

    @work
    async def _ask(self, screen: Screen[_T]) -> _T:
        """Push a screen and wait for the answer it is dismissed with.

        This exists to be a **worker context**, which is the one thing the surface did not
        have. `push_screen_wait` calls `get_current_worker()` and raises `NoActiveWorker`
        unless it is already running in a worker (`textual/app.py:2958-2964`), and every
        handler here runs on the message pump, which is not one. `in_thread` does not help:
        its worker runs the blocking call, while its *caller* still awaits from the pump.

        `exclusive` is not passed, and must not be (DEC-008): a confirmation that cancelled
        the one in flight would dismiss an unanswered modal and start a second, which is the
        cancel-on-re-entry the master gate sweeps for.

        Callers take the result with `await self._ask(screen).wait()` — the decorator returns
        a `Worker`, and it is the body that runs inside the context, so awaiting it from a
        handler is correct.
        """
        return await self.push_screen_wait(screen)

    # The transitional bridge -----------------------------------------------------

    async def _enter_legacy(self) -> None:
        """Make the transitional host the active screen, without stacking two of them.

        Only the resume flow still needs it. Every position it hosts rests directly on the
        project list, which is what the back paths those positions already had assert.
        """
        if isinstance(self.screen, LegacyScreen):
            return
        await self.switch_flow(LegacyScreen())

    def _show_projects(self) -> None:
        self.return_to_projects()

    async def legacy_choose(self, key: str) -> None:
        """Dispatch a chosen row for the positions `Step` still owns."""
        if self._step is Step.RESUME_PROJECTS:
            await self._resolve_resume_project(key)
        elif self._step is Step.RESUME_PROFILES:
            await self._resolve_resume_profile(key)
        elif self._step is Step.RESUME_CONVERSATIONS:
            await self._resolve_resume_conversation(key)
        elif self._step is Step.RESUME_CONFIRM:
            await self._resolve_resume_confirm(key)

    # Actions -------------------------------------------------------------------

    async def action_back(self) -> None:
        """Leave the current position for the one it was reached from.

        For an extracted screen this is the stack itself — pop, and the screen beneath is by
        construction where the owner came from. Two exceptions remain, and both disappear by
        the end of Stage 3: the resume flow repaints one host screen, so it has no stack to
        pop; and the two confirmations are repainted onto the session detail, so leaving one
        means redrawing the detail rather than popping away from it.
        """
        if self._busy:
            return
        screen = self.screen
        if isinstance(screen, SessionDetailScreen) and self._step in {
            Step.FORCE_CONFIRM,
            Step.REMOTE_CONTROL_CONFIRM,
        }:
            await screen.render_detail()
            return
        if isinstance(screen, LegacyScreen):
            if self._step in {Step.RESUME_PROJECTS, Step.RESUME_PROFILES}:
                self._show_projects()
            elif self._step is Step.RESUME_CONVERSATIONS:
                await self._show_resume_profiles()
            elif self._step is Step.RESUME_CONFIRM:
                await self._show_resume_conversations()
            return
        if len(self.screen_stack) > 1:
            self.pop_screen()

    async def action_refresh(self) -> None:
        """Re-read the catalogue, so a project another process created becomes selectable."""
        if self._busy:
            return
        try:
            self._catalogue = await self.in_thread(
                self._services.refresh_catalogue, group="catalogue"
            )
        except Exception:
            _LOG.exception("catalogue refresh failed")
            self._set_status("The project catalogue could not be re-read. Check this host.")
            return
        self._show_projects()

    async def action_add_project(self) -> None:
        if not self._busy:
            await self.show_areas()

    async def show_areas(self) -> None:
        await self.switch_flow(AreasScreen())

    async def action_sessions(self) -> None:
        """Show every managed session, including ones this process never launched."""
        if self._busy:
            return
        await self.show_sessions()

    async def show_sessions(self) -> None:
        screen = self.screen
        if isinstance(screen, SessionsScreen):
            await screen.reload()
            return
        await self.switch_flow(SessionsScreen())

    async def show_detail(self, session_value: str) -> None:
        """Open — or redraw — the detail for one session.

        Redrawing rather than pushing when the same detail is already on screen is what keeps
        a stop, a confirmation abort, or a remote-control change from growing the stack by one
        screen every time the owner uses it.
        """
        screen = self.screen
        if isinstance(screen, SessionDetailScreen) and screen.session_value == session_value:
            await screen.render_detail()
            return
        await self.push_screen(SessionDetailScreen(session_value))

    # Resume ---------------------------------------------------------------------

    async def action_resume(self) -> None:
        """Open the resume flow, if this host wired a conversation service at all."""
        if self._busy or self._services.conversations is None:
            return
        await self._enter_legacy()
        self._resume_project = None
        self._resume_profile = None
        self._resume_choice = None
        self._step = Step.RESUME_PROJECTS
        self._hide_entry()
        self._set_status("Resume a conversation. Choose its project.")
        self._fill(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}") for project in self._catalogue
            )
            or ((_CANCEL, "No projects available"),)
        )

    async def _resolve_resume_project(self, key: str) -> None:
        project = next((item for item in self._catalogue if item.opaque_id == key), None)
        if project is None:
            # Stay in the resume flow, as the launch picker does for the same failure,
            # rather than dropping the owner into a different wizard with no explanation.
            self._set_status("That project is no longer available. Refresh and try again.")
            return
        self._resume_project = project
        # Guarded across the await for the reason Stage 3 established: a second entry point
        # firing mid-navigation used to reset the chosen project, after which selecting a
        # profile silently did nothing and only Escape recovered.
        self._busy = True
        try:
            await self._show_resume_profiles()
        finally:
            self._busy = False

    async def _show_resume_profiles(self) -> None:
        """Offer only profiles that report themselves resume-capable (DEC-002).

        Capability comes from `capabilities()`, which reports what each provider can
        actually do on this host — never from a version allowlist.
        """
        conversations = self._services.conversations
        if conversations is None:
            return
        try:
            capabilities = await conversations.capabilities()
        except Exception as error:
            _LOG.exception("resume capabilities failed")
            self._set_status(f"Resume is unavailable: {error}")
            self._fill(((_BACK, "Back"),))
            return
        capable = tuple(
            capability
            for capability in capabilities
            if capability.catalogue_available and capability.selected_resume_available
        )
        self._step = Step.RESUME_PROFILES
        if not capable:
            self._set_status("No agent on this host can resume a saved conversation.")
            self._fill(((_BACK, "Back"),))
            return
        self._set_status("Choose the agent whose conversation you want to resume.")
        self._fill(
            tuple((str(item.profile_id), str(item.profile_id)) for item in capable)
            + ((_BACK, "Back"),)
        )

    async def _resolve_resume_profile(self, key: str) -> None:
        if key == _BACK:
            await self.action_resume()
            return
        if not any(profile.profile_id == key for profile in self._services.profiles):
            # Defence in depth, matching the launch picker: the rows here are already
            # filtered to resume-capable profiles, so a key naming another one is stale.
            self._set_status("That agent is not available on this host.")
            return
        self._resume_profile = key
        self._resume_page = 1
        self._busy = True
        try:
            await self._show_resume_conversations()
        finally:
            self._busy = False

    async def _show_resume_conversations(self) -> None:
        """One bounded page of safe metadata; provider IDs never leave the server."""
        conversations = self._services.conversations
        if conversations is None or self._resume_profile is None or self._resume_project is None:
            return
        try:
            page = await conversations.catalogue(
                ConversationCatalogueQuery(
                    profile_id=ProfileId(self._resume_profile),
                    project_id=ProjectId(self._resume_project.opaque_id),
                    page=self._resume_page,
                    page_size=_RESUME_PAGE_SIZE,
                )
            )
        except Exception as error:
            _LOG.exception("conversation catalogue failed")
            self._set_status(f"The conversations could not be listed: {error}")
            self._fill(((_BACK, "Back"),))
            return
        self._step = Step.RESUME_CONVERSATIONS
        self._resume_page_count = page.page_count
        if page.unavailable_reason is not None:
            self._set_status(f"Conversations are unavailable: {page.unavailable_reason}")
            self._fill(((_BACK, "Back"),))
            return
        if not page.conversations:
            self._set_status("There are no saved conversations for that agent and project.")
            self._fill(((_BACK, "Back"),))
            return
        entries = [(str(item.reference), conversation_row(item)) for item in page.conversations]
        if page.page > 1:
            entries.append((_PREVIOUS, "Previous page"))
        if page.page < page.page_count:
            entries.append((_NEXT, "Next page"))
        entries.append((_BACK, "Back"))
        self._set_status(f"Choose a conversation. Page {page.page} of {page.page_count}.")
        self._fill(tuple(entries))

    async def _resolve_resume_conversation(self, key: str) -> None:
        conversations = self._services.conversations
        if conversations is None:
            return
        if key == _BACK:
            await self._show_resume_profiles()
            return
        if key in {_NEXT, _PREVIOUS}:
            step = 1 if key == _NEXT else -1
            self._resume_page = max(1, min(self._resume_page + step, self._resume_page_count))
            self._busy = True
            try:
                await self._show_resume_conversations()
            finally:
                self._busy = False
            return
        try:
            # The reference is only ever one this surface rendered from a server-issued
            # page; constructing it here re-validates its opaque shape, and resolution is
            # server-side, so a forged or stale value resolves to nothing rather than a path.
            resolved = await conversations.resolve_for_resume(ConversationReference(key))
        except ValueError:
            self._set_status("That conversation selection is not valid.")
            return
        except Exception as error:
            _LOG.exception("conversation resolve failed")
            self._set_status(f"That conversation could not be resolved: {error}")
            return
        if resolved is None:
            self._set_status("That conversation is no longer available.")
            return
        self._resume_choice = resolved
        self._step = Step.RESUME_CONFIRM
        self._set_status(
            f"Resume {conversation_row(resolved.summary)}\n"
            f"Agent: {self._resume_profile}\n"
            "This starts a new managed session continuing that conversation."
        )
        self._fill(((_CANCEL, "Cancel"), ("resume-confirm", "Resume it")))

    async def _resolve_resume_confirm(self, key: str) -> None:
        if key != "resume-confirm" or self._resume_choice is None:
            await self.action_resume()
            return
        if self._resume_project is None or self._resume_profile is None:
            await self.action_resume()
            return
        self._busy = True
        try:
            record = await self._services.launcher.resume(
                ResumeCommand(
                    ProjectId(self._resume_project.opaque_id),
                    ProfileId(self._resume_profile),
                    self._resume_choice,
                    _idempotency_key(),
                )
            )
        except Exception as error:
            _LOG.exception("resume failed")
            self._fill(((_BACK, "Back"),))
            self._set_status(f"The conversation was not resumed: {error}")
            return
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            self._fill(((_BACK, "Back"),))
            self._set_status(
                "The resumed session did not become ready, but its pane may still exist. "
                "Reach it with:\n"
                f"{' '.join(self._services.attach_argv(str(record.session_id)))}"
            )
            return
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_argv(session_id)))

    # The destructive path, still repainted onto the session detail until Stage 3 ---------

    async def confirm_remote_control(self) -> None:
        """Ask before changing a live pane's control mode, re-checking the policy first."""
        session_value = self.detail_session
        if session_value is None:
            return
        # Guarded across the read, not just around the command. Without this, `action_back`
        # sees an open guard and a step that has not flipped yet, so a plain Escape during
        # the await pops the detail — and the confirmation then paints its rows onto
        # whatever screen was underneath it. `stop` has always held the guard for this
        # reason; building the dialog needs it just as much now that leaving the position
        # means leaving the *screen*.
        self._busy = True
        try:
            record = await self.current_record(session_value)
        finally:
            self._busy = False
        if record is None:
            self._set_status("That session is no longer available.")
            return
        if not remote_control_available(record):
            self._set_status(
                f"{record.display.rendered}\n"
                "Remote Control is not available for this session.\n"
                f"{explain_state(record.state)}"
            )
            return
        self._step = Step.REMOTE_CONTROL_CONFIRM
        self._hide_entry()
        self._set_status(
            f"Claude Remote Control for {record.display.rendered}\n"
            "Enabling lets this session be driven remotely; disabling returns it to local "
            "control only."
        )
        self._fill(
            (
                (_CANCEL, "Cancel"),
                ("remote-control-active", "Enable Remote Control"),
                ("remote-control-inactive", "Disable Remote Control"),
            )
        )

    async def resolve_remote_control(self, key: str) -> None:
        desired = {
            "remote-control-active": RemoteControlState.ACTIVE,
            "remote-control-inactive": RemoteControlState.INACTIVE,
        }.get(key)
        session_value = self.detail_session
        if desired is None or session_value is None:
            if session_value is not None:
                await self.show_detail(session_value)
            return
        self._busy = True
        try:
            record = await self.current_record(session_value)
            if record is None:
                self._set_status("That session is no longer available.")
                return
            if not remote_control_available(record):
                self._set_status("Remote Control is no longer available for this session.")
                return
            state = await self._services.launcher.set_remote_control(
                RemoteControlCommand(record.session_id, desired, _idempotency_key())
            )
        except Exception as error:
            _LOG.exception("remote control failed")
            # Same reason as the failed stop: do not leave the cursor resting on the
            # button that just failed, or a second enter re-issues it as a blind retry.
            self._fill(((_BACK, "Back"),))
            self._set_status(
                f"Remote Control was not changed: {error}\n"
                "Go back and open the session again to see its current state."
            )
            return
        else:
            # Held for the same reason as `stop`'s refresh: nothing else may run until
            # the result is on screen. These calls are synchronous today, so the window
            # is empty — the guard is here so it stays empty if one of them ever awaits.
            self._step = Step.SESSION_DETAIL
            self._set_status(f"{record.display.rendered}\nRemote Control: {state.value}")
            self._fill(self.body.detail_entries(record))  # type: ignore[attr-defined]
        finally:
            self._busy = False

    async def confirm_force(self) -> None:
        """Ask a second time, on its own position, with abort as the resting choice.

        Force kills a running agent and cannot be undone, so it is deliberately not
        reachable by repeating whatever keystroke opened the detail: the abort entry is
        first and highlighted, and confirming means moving to a different row on purpose.
        """
        session_value = self.detail_session
        if session_value is None:
            return
        # Guarded for the reason given on `confirm_remote_control`: an Escape landing inside
        # this read would otherwise pop the detail and leave the force confirmation painted
        # onto the screen beneath it.
        self._busy = True
        try:
            record = await self.current_record(session_value)
        finally:
            self._busy = False
        if record is None:
            self._set_status("That session is no longer available.")
            return
        self._step = Step.FORCE_CONFIRM
        self._hide_entry()
        self._set_status(
            f"Force stop {record.display.rendered}?\n"
            "This kills the agent immediately and cannot be undone. Any work it has not "
            "saved is lost.\n"
            f"{explain_state(record.state)}"
        )
        self._fill(((_CANCEL, "Cancel"), ("force-confirm", "Yes, force stop it")))

    async def resolve_force_confirm(self, key: str) -> None:
        if key == "force-confirm":
            await self.stop(FORCE)
            return
        # Anything else -- cancel, back, or an unrecognized key -- aborts without issuing.
        session_value = self.detail_session
        if session_value is not None:
            await self.show_detail(session_value)

    async def stop(self, action: str) -> None:
        """Issue one stop, after re-reading the record and re-checking the policy.

        The policy is consulted again here rather than trusted from the rendered entry: the
        session may have moved on since the list was drawn, and an action that was legal
        then can be illegal now. The service would refuse it anyway — this keeps the owner
        from seeing an exception instead of an explanation.
        """
        session_value = self.detail_session
        if session_value is None or self._busy:
            return
        self._busy = True
        try:
            record = await self.current_record(session_value)
            if record is None:
                self._set_status("That session is no longer available.")
                return
            if action not in available_actions(record.state):
                self._set_status(
                    f"{record.display.rendered}\n"
                    f"{_ACTION_LABELS[action]} is no longer available for this session.\n"
                    f"{explain_state(record.state)}"
                )
                return
            await self._issue_stop(action, record)
        except Exception as error:
            _LOG.exception("stop failed")
            # Move the cursor off the confirm button before reporting. A failed force
            # leaves the owner resting on "Yes, force stop it", so without this a second
            # enter re-issues the kill as a retry nobody deliberately chose.
            self._fill(((_BACK, "Back"),))
            self._set_status(
                f"{_ACTION_LABELS[action]} did not complete: {error}\n"
                "The session was left as it is. Go back and open it again to see its "
                "current state, then retry if you still want to."
            )
            return
        else:
            # Inside the guard on purpose. `_busy` means "no other action may run until
            # this one's result is on screen" — and the redraw awaits, so releasing first
            # leaves a window where the position has flipped but the list still holds the
            # previous screen's entries.
            await self.show_detail(session_value)
        finally:
            self._busy = False

    async def _issue_stop(self, action: str, record: SessionRecord) -> None:
        """Send exactly one curated command; the commands themselves carry no arguments."""
        launcher = self._services.launcher
        if action == GRACEFUL:
            await launcher.graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))
        elif action == CLEANUP:
            await launcher.cleanup(CleanupCommand(record.session_id))
        else:
            await launcher.force_stop(ForceStopCommand(record.session_id))

    # Store reads screens share ---------------------------------------------------

    def report_store_failure(self, error: Exception) -> None:
        """Report a failed store or terminal read without tearing the surface down.

        Every read this surface makes can fail: the store has a second writer, and a
        recovery surface is used precisely when things are already broken. Losing the app
        to an exception is the one outcome that leaves the owner with nothing.
        """
        _LOG.exception("session read failed", exc_info=error)
        self._fill(((_BACK, "Back"),))
        self._set_status(
            f"The managed sessions could not be read: {error}\n"
            "Press escape to return to the project list."
        )

    async def current_record(self, session_value: str) -> SessionRecord | None:
        """Re-read one session from the store, without refreshing readiness.

        The re-read is what detects a session stopped by the other writer while this list
        was on screen. The *refresh* is deliberately not repeated: it rescans every record
        and runs a tmux capture per FAILED session, so repeating it on each navigation
        would make opening a detail and copying its attach command cost three full passes.
        The bot refreshes once per list open for the same reason.
        """
        for record in await self.read_sessions():
            if str(record.session_id) == session_value:
                return record
        return None

    async def load_sessions(self) -> tuple[SessionRecord, ...]:
        """Refresh readiness, then return the sessions worth showing.

        Order is whatever the store returns; nothing here sorts, and the row's age column
        is what tells the owner how old a session is.
        """
        await self._services.launcher.refresh_readiness()
        return await self.read_sessions()

    async def read_sessions(self) -> tuple[SessionRecord, ...]:
        """List the store's sessions, filtering what no surface can act on."""
        records = await self._services.launcher.list_sessions()
        # ENDED is filtered exactly as the bot filters it: the record is retained for audit
        # but there is nothing left to reach, inspect, or stop.
        return tuple(record for record in records if record.state is not SessionState.ENDED)

    async def launch(self) -> str | None:
        """Issue the gathered launch, and return what to say if it did not take.

        Returning the message rather than rendering it keeps the screen that owns the
        review in charge of its own rows: a failure has to leave the cursor somewhere
        deliberate, and only the review screen knows where that is.
        """
        project, profile = self.selection.project, self.selection.profile
        if project is None or profile is None:
            self.return_to_projects()
            return None
        self._busy = True
        self._set_status("Launching…")
        try:
            record = await self._services.launcher.launch(
                LaunchCommand(
                    ProjectId(project.opaque_id),
                    ProfileId(profile.profile_id),
                    _idempotency_key(),
                    self.selection.label,
                )
            )
        except Exception as error:
            _LOG.exception("launch failed")
            return f"The session was not started: {error}\n{self.selection.review()}"
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            return (
                "The session did not become ready, but its pane may still exist. Reach it "
                "with:\n"
                f"{' '.join(self._services.attach_argv(str(record.session_id)))}\n"
                "Check this host before retrying, or a second session will run alongside it.\n"
                f"{self.selection.review()}"
            )
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_argv(session_id)))
        return None


def _idempotency_key() -> str:
    from uuid import uuid4

    return f"tui-{uuid4()}"


def run_local_terminal(
    context: TuiContext,
    *,
    runner: Callable[[TuiContext], AttachRequest | None] | None = None,
) -> AttachRequest | None:
    """Run the terminal surface and return what the caller must attach to, if anything."""
    return runner(context) if runner is not None else RemoteAgentsTui(context).run()
