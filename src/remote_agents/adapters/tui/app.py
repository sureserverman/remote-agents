"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TypeVar, cast

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.worker import WorkerCancelled, WorkerFailed

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.model import (
    _BACK,
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
    ProjectsScreen,
    ResumeProjectsScreen,
    SessionDetailScreen,
    SessionsScreen,
)
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import ConfirmScreen
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import (
    ACTION_LABELS as _ACTION_LABELS,
)
from remote_agents.application.session_actions import (
    CLEANUP,
    GRACEFUL,
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.domain.conversations import ResolvedConversation
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
    "TuiContext",
    "age",
    "conversation_row",
    "label_or_error",
    "run_local_terminal",
    "selectable_area",
    "session_row",
]


_RESUME_PAGE_SIZE = 10


class RemoteAgentsTui(App[AttachRequest | None]):
    """Choose a project and an agent, launch it, and hand back an attach command."""

    # Scoped to `ChoiceScreen`, not to every `OptionList` in the app. A bare type selector
    # here also reached the confirmation modal's list, and app CSS outranks a screen's own
    # `DEFAULT_CSS` whatever the selector's specificity — so `1fr` won over the modal's
    # `height: auto` and stretched a two-row dialog down the whole terminal. The rule was
    # always about the body every position renders; this says so.
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    ChoiceScreen #status { height: auto; padding: 0 1; }
    ChoiceScreen OptionList { height: 1fr; }
    ChoiceScreen #output { height: 1fr; padding: 0 1; }
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
        self._busy = False

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
    def body(self) -> ChoiceScreen:
        """The active screen, typed as the body every position renders."""
        return cast(ChoiceScreen, self.screen)

    def return_to_projects(self) -> None:
        """Unwind to the resting position, whatever the owner had pushed on top of it."""
        while len(self.screen_stack) > 1:
            self.pop_screen()
        screen = self.screen
        if isinstance(screen, ProjectsScreen):
            screen.render_projects()

    async def go_back(self) -> None:
        """Pop one position, and let the screen it reveals re-read what it shows.

        The single pop in the surface. Routing every back path through it is what lets the
        refresh be *awaited*: a screen that becomes visible again gets its `on_reveal` before
        this returns, so a caller holding the busy guard still holds it while the revealed
        rows are redrawn. Textual's own `ScreenResume` would run after the pop returned,
        which is outside that guard.

        A pop is refused rather than raising when the resting position is all that is left:
        `pop_screen` raises `ScreenStackError` on the last screen, and escape at rest is
        meant to be inert.
        """
        if len(self.screen_stack) <= 1:
            return
        self.pop_screen()
        revealed = self.screen
        if isinstance(revealed, ChoiceScreen):
            await revealed.on_reveal()

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
        handler is correct. Confirmations go through `ask_to_confirm` rather than calling this
        directly, so there is one place that decides what a cancelled worker means.
        """
        return await self.push_screen_wait(screen)

    async def ask_to_confirm(self, modal: ConfirmScreen) -> bool:
        """Put one modal question in front of the owner and answer what they said.

        The single entry point for every destructive confirmation, which is what makes
        "a destructive call is reachable only after a modal returned `True`" a property one
        can look for rather than a convention spread across the screens that issue commands.

        A cancelled worker is `False`, never an answer. `_ask`'s worker is owned by the app,
        so it is cancelled when the app is shutting down — and the one reading of "the app
        went away mid-question" that must never be reachable is consent. `WorkerFailed` is
        unwrapped for the same reason `in_thread` unwraps it: the caller reports the failure,
        and "Worker raised exception: …" is not what the owner needs to read.
        """
        try:
            return bool(await self._ask(modal).wait())
        except WorkerCancelled:
            return False
        except WorkerFailed as failure:
            raise failure.error from None

    # Actions -------------------------------------------------------------------

    async def action_back(self) -> None:
        """Leave the current position for the one it was reached from.

        The stack is the whole answer now — pop, and the screen beneath is by construction
        where the owner came from. There are no exceptions left: the last two, the destructive
        confirmations, are screens of their own as of this task.
        """
        if self._busy:
            return
        await self.go_back()

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
            self.body.set_status("The project catalogue could not be re-read. Check this host.")
            return
        self.return_to_projects()

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
        await self.switch_flow(ResumeProjectsScreen())

    async def issue_resume(
        self,
        screen: ChoiceScreen,
        project: CatalogProject,
        profile: str,
        resolved: ResolvedConversation,
    ) -> None:
        """Start the resumed session, and hand back the attach command if it is ready.

        On the app rather than on the confirm screen for the same reason `launch` is: it ends
        in `self.exit(...)`, which is the app's to do. What stays with the screen is where a
        failure leaves the cursor, so the message is rendered onto `screen`.
        """
        self._busy = True
        try:
            record = await self._services.launcher.resume(
                ResumeCommand(
                    ProjectId(project.opaque_id),
                    ProfileId(profile),
                    resolved,
                    _idempotency_key(),
                )
            )
        except Exception as error:
            _LOG.exception("resume failed")
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status(f"The conversation was not resumed: {error}")
            return
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status(
                "The resumed session did not become ready, but its pane may still exist. "
                "Reach it with:\n"
                f"{' '.join(self._services.attach_argv(str(record.session_id)))}"
            )
            return
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_argv(session_id)))

    # The destructive path ---------------------------------------------------------
    #
    # The two confirmations are modals in `screens/confirm.py`, answered through
    # `ask_to_confirm` before either of these is called at all. What is left here is the part
    # that talks to the service: re-read, re-check, issue once, and refresh whatever screen
    # asked. The re-read is *not* redundant with the modal — it is DEC-007's fourth
    # mitigation, and the window it covers is precisely the one the modal opens, since the
    # owner may deliberate for as long as they like while the other writer moves the session
    # on underneath them.

    async def set_remote_control(
        self, session_value: str, desired: RemoteControlState, screen: ChoiceScreen
    ) -> None:
        """Change one session's control mode, after re-reading and re-checking the policy."""
        self._busy = True
        try:
            record = await self.current_record(session_value)
            if record is None:
                screen.set_status("That session is no longer available.")
                return
            if not remote_control_available(record):
                screen.set_status("Remote Control is no longer available for this session.")
                return
            state = await self._services.launcher.set_remote_control(
                RemoteControlCommand(record.session_id, desired, _idempotency_key())
            )
        except Exception as error:
            _LOG.exception("remote control failed")
            # Same reason as the failed stop: do not leave the cursor resting on the
            # button that just failed, or a second enter re-issues it as a blind retry.
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status(
                f"Remote Control was not changed: {error}\n"
                "Go back and open the session again to see its current state."
            )
            return
        else:
            # Held for the same reason as `stop`'s refresh: nothing else may run until the
            # result is on screen, and leaving the confirmation awaits the detail's re-read.
            await screen.after_command()
            # Deliberately `self.body` and not `screen`: `after_command` has just left the
            # confirmation, so the position now showing is the detail beneath it, and that is
            # where the new control mode belongs. `screen` at this point is the popped dialog.
            self.body.set_status(f"{record.display.rendered}\nRemote Control: {state.value}")
        finally:
            self._busy = False

    async def stop(self, action: str, session_value: str, screen: ChoiceScreen) -> None:
        """Issue one stop, after re-reading the record and re-checking the policy.

        The policy is consulted again here rather than trusted from the rendered entry: the
        session may have moved on since the list was drawn, and an action that was legal
        then can be illegal now. The service would refuse it anyway — this keeps the owner
        from seeing an exception instead of an explanation. This re-read-and-recheck is
        DEC-007's fourth mitigation and it is why a stale row cannot issue a stale command.

        `screen` is whichever position asked, so a failure reports where the owner is looking.
        Since the confirmation became a modal that is dismissed by the answer, that is the
        session detail for every action including force — there is no confirmation screen
        still on the stack by the time this runs.
        """
        if self._busy:
            return
        self._busy = True
        try:
            record = await self.current_record(session_value)
            if record is None:
                screen.set_status("That session is no longer available.")
                return
            if action not in available_actions(record.state):
                screen.set_status(
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
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status(
                f"{_ACTION_LABELS[action]} did not complete: {error}\n"
                "The session was left as it is. Go back and open it again to see its "
                "current state, then retry if you still want to."
            )
            return
        else:
            # Inside the guard on purpose. `_busy` means "no other action may run until this
            # one's result is on screen", and both branches await a re-read, so releasing
            # first would leave a window where the command has landed but the rows still
            # describe the session as it was.
            await screen.after_command()
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
        self.body.show_choices(((_BACK, "Back"),))
        self.body.set_status(
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
        self.body.set_status("Launching…")
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
