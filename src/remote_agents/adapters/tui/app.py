"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TypeVar

from textual import work
from textual.app import App, ScreenStackError
from textual.binding import Binding
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.worker import WorkerCancelled, WorkerFailed

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.model import (
    _BACK,
    AttachRequest,
    LaunchFailure,
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
    FORCE,
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
    "LaunchFailure",
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
    ChoiceScreen #status { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    ChoiceScreen OptionList { height: 1fr; }
    ChoiceScreen #output { height: 1fr; padding: 0 1; }
    """
    #: Shown in the header, with each screen's breadcrumb as the sub-title beside it. Set
    #: rather than left to default: `App.title` falls back to the class name, so the header
    #: read "RemoteAgentsTui" — the one string on screen that named an implementation detail.
    TITLE = "Remote Agents"
    # Every one of these is answered per screen by `ChoiceScreen.check_action`, which hides
    # the ones that would do nothing here. The tooltips say what the key does *to the thing
    # the owner is looking at*, because the labels alone were ambiguous in the one way that
    # mattered: "Refresh" gave no hint that it used to abandon the position it was pressed on.
    BINDINGS = [
        Binding("escape", "back", "Back", tooltip="Return to the position you came from"),
        Binding(
            "ctrl+r",
            "refresh",
            "Refresh",
            tooltip="Re-read what this screen shows, without leaving it",
        ),
        Binding(
            "ctrl+n", "add_project", "Add project", tooltip="Register a new project directory"
        ),
        Binding("ctrl+s", "sessions", "Sessions", tooltip="Every managed session on this host"),
        Binding(
            "ctrl+o", "resume", "Resume", tooltip="Reopen a saved conversation as a new session"
        ),
        Binding("ctrl+q", "quit", "Quit", tooltip="Leave the terminal surface"),
    ]

    def __init__(self, context: TuiContext) -> None:
        super().__init__()
        self._services = context
        self._catalogue = context.catalogue
        self.selection = LaunchSelection()
        self._busy = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Ask the position on screen whether one of *these* bindings applies to it.

        **This delegation is required, and the reason is a framework detail worth stating.**
        `Screen.active_bindings` — what `Footer` renders from — resolves each binding through
        `App._check_action_state(binding.action, namespace)`, and the namespace is where the
        binding was *declared*. These six are declared on the app, so Textual asks the **app**
        about them and never the screen. A `check_action` written only on `ChoiceScreen` is
        therefore consulted for screen-declared bindings and silently ignored for every one of
        these, which is the whole set the footer was over-advertising.

        The per-screen answer is still where the knowledge lives — `ChoiceScreen.check_action`
        mirrors each action's own early return, and `ConfirmScreen` hides the lot — so this is
        a router, not a second rule set. Screens that answer for themselves are asked; anything
        else gets the permissive default, which is the honest answer for a screen this app did
        not write.
        """
        try:
            screen = self.screen
        except ScreenStackError:
            # `App.screen` raises on an empty stack. Nothing here can empty it — `go_back` and
            # `return_to_projects` both stop at depth 1, and `switch_flow` pops to 1 before it
            # pushes — but this runs from a footer redraw, and an exception out of that path is
            # the class that already killed this app once. Textual's own `App.refresh` guards
            # the identical dereference; the first version of this guard checked two conditions
            # that could never be false and left this one open.
            return True
        return screen.check_action(action, parameters)

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

    def announce(self, message: str, *, severity: SeverityLevel = "error") -> None:
        """Announce something that did not happen, without taking the position off screen.

        The one place `notify` is called from in this surface, so `markup=False` is decided
        once rather than at each site. Every message routed here interpolates a string this
        app did not author — an exception's text, a provider's reason, the owner's own label —
        and `Toast` renders console markup by default, where an unbalanced `[` raises
        `MarkupError`. That is not a hypothetical for this codebase: it is the same defect
        `markup=False` on `#status` and `#choices` was added for, found there in three
        separate sources because each call site was escaping for itself.

        `severity` is the honest one of Textual's three rather than always `error`. A stop the
        policy now refuses and a stop that raised are both "it did not happen", but only one
        of them is a fault — and an owner who sees the same red for both learns to read
        neither.
        """
        self.notify(message, severity=severity, markup=False)

    @property
    def body(self) -> ChoiceScreen | None:
        """The active screen when it is one that renders the shared body, otherwise `None`.

        This was an unchecked `cast` to `ChoiceScreen` — the last vestige of the single
        repainted-body model, and an unchecked one. BL-021 recorded it at the Stage 2 gate as
        safe *for now* and reachable as soon as this stage landed a `ModalScreen`, which is
        exactly what happened: a confirmation is not a `ChoiceScreen`, it has no
        `show_choices`, and the cast would have handed one to `report_store_failure` to call
        it on.

        Answering `None` rather than raising, because every caller here is already on a
        failure path — reporting a catalogue read that failed, or a store read that failed —
        and the one thing those must not do is raise again. A message that cannot be rendered
        because the owner is looking at a modal is a message that will be re-rendered by the
        re-read on the way back; losing the app instead is what this surface exists not to do.
        """
        screen = self.screen
        return screen if isinstance(screen, ChoiceScreen) else None

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

    @work(exit_on_error=False)
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

        `exit_on_error=False` for the same reason `in_thread` passes it, and it was missing
        here until an evaluator built a modal that raises and watched the app die: the
        decorator's default is to take the app down on an unhandled exception, so the
        `except Exception` its callers wrap this in — written precisely because an escaping
        exception exits the app — could never have run. A confirmation that cannot be drawn
        must leave the owner on the detail with an explanation, not with nothing.

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
        """Re-run the active screen's own load, and leave the owner where they are.

        This used to re-read the catalogue and then `return_to_projects()` unconditionally,
        which made Refresh a *navigation*: pressed on the sessions list — the one position
        whose answer goes stale on its own, because a second process writes the same store —
        it abandoned that list and dropped the owner on the project picker. What each
        position has to re-read is the position's own question, so it is asked here and
        answered by `ChoiceScreen.refresh_contents`, whose default is to do nothing.

        The stack is deliberately untouched. Nothing about "re-read what I am looking at"
        implies moving, and the catalogue re-read the projects list still wants is now its
        own `refresh_contents` rather than something every other screen inherits.
        """
        if self._busy:
            return
        screen = self.body
        if screen is None:
            return
        await screen.refresh_contents()

    async def reload_catalogue(self) -> bool:
        """Re-read the project catalogue into app state, and say whether that took.

        On the app because the catalogue is app state — `self._catalogue` is what every
        screen reads through `catalogue` — while the two callers are screens: the project
        list refreshing itself, and the add-project review pulling in the project it just
        created before unwinding to that list.

        Reporting is the caller's, which is why this answers a bool rather than rendering.
        The two failures want different words in different places: one is "the list you are
        looking at is stale", the other is "the project was created but the list may not show
        it yet", and only the screen that asked knows which of those it is about to say.
        """
        try:
            self._catalogue = await self.in_thread(
                self._services.refresh_catalogue, group="catalogue"
            )
        except Exception:
            _LOG.exception("catalogue refresh failed")
            return False
        return True

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
            screen.set_status("Nothing was started. Go back and try again.")
            screen.announce(f"The conversation was not resumed: {error}")
            return
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            # The command stays in the status line and the explanation goes to the toast,
            # which is the split this whole task turns on applied to its hardest case: the
            # owner has to be able to *copy* the attach command, and a toast expires. What
            # they need to keep is the artifact; what they need to be told once is why they
            # are being handed it.
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status(
                f"Attach with: {' '.join(self._services.attach_argv(str(record.session_id)))}"
            )
            screen.announce(
                "The resumed session did not become ready, but its pane may still exist. "
                "The command below reaches it."
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
    #
    # **Every branch where that re-read disagrees with the rows now redraws before returning**,
    # and the rule is worth stating once rather than per site because the sites are spread
    # across two files. A re-read that finds the session gone, or in a state the policy no
    # longer offers this action for, has established that the rows in front of the owner are
    # wrong — they were built from the earlier read. Reporting and returning left them there,
    # still offering a stop for a session that had ended. The success path has always ended in
    # `after_command`; these are the ten branches that did not — four here (`stop` and
    # `set_remote_control`, a vanished record and a policy refusal each) and six in
    # `screens/sessions.py` (the same pair on each confirm method, plus the vanished-record
    # read in `show_attach` and in `show_inspect`). Found as a class after a Tier-1 review
    # named two of them.



    async def set_remote_control(
        self, session_value: str, desired: RemoteControlState, screen: ChoiceScreen
    ) -> None:
        """Change one session's control mode, after re-reading and re-checking the policy.

        Unlike `stop`, this does not open with `if self._busy: return`. The asymmetry is real
        and worth knowing about: what refuses a second concurrent change is
        `ChoiceScreen.on_option_list_option_selected`, which drops a row selection while the
        surface is busy, so the refusal happens before this is ever called. That holds for the
        one caller there is — `SessionDetailScreen.confirm_remote_control`, reached only
        through that handler. A second caller reaching this directly would not be refused
        here, which is the thing to check before adding one.
        """
        self._busy = True
        try:
            record = await self.current_record(session_value)
            if record is None:
                screen.announce("That session is no longer available.", severity="warning")
                await screen.after_command()
                return
            if not remote_control_available(record):
                screen.announce(
                    "Remote Control is no longer available for this session.", severity="warning"
                )
                await screen.after_command()
                return
            state = await self._services.launcher.set_remote_control(
                RemoteControlCommand(record.session_id, desired, _idempotency_key())
            )
        except Exception as error:
            _LOG.exception("remote control failed")
            # Same reason as the failed stop: do not leave the cursor resting on the
            # button that just failed, or a second enter re-issues it as a blind retry.
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status("Go back and open the session again to see its current state.")
            screen.announce(f"Remote Control was not changed: {error}")
            return
        else:
            # Inside the guard on purpose, as in `stop`: nothing else may run until the
            # result is on screen, and this awaits a re-read.
            await screen.after_command()
            # Reported onto `screen`, which is the session detail — the confirmation was a
            # modal and was dismissed by the answer, so there is no dialog left on the stack
            # for this to land on by mistake. This line used to read `self.body` with a
            # comment explaining that `screen` was the popped confirmation and `self.body`
            # the detail beneath it. That distinction was real when the confirmation was a
            # pushed screen and is gone now: both expressions resolve to the same detail, and
            # naming it once is what stops the next reader from looking for the dialog.
            #
            # The order matters and survives the change: `after_command` re-reads the detail,
            # which rewrites the status from the store, so this has to come after it or the
            # new control mode would be painted and then immediately overwritten.
            #
            # The session's own name is dropped from this line because the header carries it
            # now — that is what the breadcrumb is for, and repeating it here is what made
            # this a two-line message in a one-line region.
            screen.set_status(f"Remote Control: {state.value}")
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
                screen.announce("That session is no longer available.", severity="warning")
                await screen.after_command()
                return
            if action not in available_actions(record.state):
                # A refusal, not a fault: the session moved on between the row being drawn
                # and the key being pressed, which is the window DEC-007's re-read exists to
                # catch. `warning` rather than `error` says so.
                screen.announce(
                    f"{_ACTION_LABELS[action]} is no longer available for this session. "
                    f"{explain_state(record.state)}",
                    severity="warning",
                )
                await screen.after_command()
                return
            await self._issue_stop(action, record)
        except Exception as error:
            _LOG.exception("stop failed")
            # Move the cursor off the confirm button before reporting. A failed force
            # leaves the owner resting on "Yes, force stop it", so without this a second
            # enter re-issues the kill as a retry nobody deliberately chose.
            screen.show_choices(((_BACK, "Back"),))
            screen.set_status("Go back and open the session again to see its current state.")
            screen.announce(
                f"{_ACTION_LABELS[action]} did not complete: {error} "
                "The session was left as it is; retry if you still want to."
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
        """Send exactly one curated command; the commands themselves carry no arguments.

        Force is its own named branch and an unrecognized action raises, rather than the kill
        being the trailing `else`. It was, and nothing could reach it — `stop` only calls this
        for an action `available_actions` returned. But "anything I do not recognize is a
        kill" is a fail-dangerous default in the one method that kills, and the cost of it
        being right is that every future caller stays correct by accident.
        """
        launcher = self._services.launcher
        if action == GRACEFUL:
            await launcher.graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))
        elif action == CLEANUP:
            await launcher.cleanup(CleanupCommand(record.session_id))
        elif action == FORCE:
            await launcher.force_stop(ForceStopCommand(record.session_id))
        else:
            raise ValueError(f"no command is curated for the action {action!r}")

    # Store reads screens share ---------------------------------------------------

    def report_store_failure(self, error: Exception, screen: ChoiceScreen | None = None) -> None:
        """Report a failed store or terminal read onto the screen that asked, or nowhere.

        Every read this surface makes can fail: the store has a second writer, and a
        recovery surface is used precisely when things are already broken. Losing the app
        to an exception is the one outcome that leaves the owner with nothing.

        `screen` is the position whose read failed, and it is reported onto **only if it is
        still the one showing**. Without that check this rendered onto whatever was on top:
        a read that failed after the asking screen had been left replaced the *project
        list's* rows with a lone "Back" and told the owner to "press escape to return to the
        project list" — at the project list, where escape is inert. Every screen in this
        package already guards its own renders with `showing` for exactly this reason; this
        was the one render path that reached around it, and it is the path that runs when
        things are already going wrong.

        Reporting nowhere is the right outcome when the asker has been left: the message
        describes a read the owner is no longer waiting on, the log line above is the durable
        record, and the position they *are* looking at re-reads on its own terms.
        """
        _LOG.exception("session read failed", exc_info=error)
        target = screen if screen is not None else self.body
        if target is None or not target.showing:
            return
        target.show_choices(((_BACK, "Back"),))
        target.set_status("Press escape to return to the project list.")
        target.announce(f"The managed sessions could not be read: {error}")

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

    async def launch(self) -> LaunchFailure | None:
        """Issue the gathered launch, and return what to say if it did not take.

        Returning the message rather than rendering it keeps the screen that owns the
        review in charge of its own rows: a failure has to leave the cursor somewhere
        deliberate, and only the review screen knows where that is.

        A `LaunchFailure` rather than a string since the status split: the two failures below
        both have something the owner keeps and something they read once, and only the review
        screen can put the first of those anywhere.
        """
        project, profile = self.selection.project, self.selection.profile
        if project is None or profile is None:
            self.return_to_projects()
            return None
        self._busy = True
        if (body := self.body) is not None:
            body.set_status("Launching…")
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
            return LaunchFailure(
                status="Nothing was started. Launch again, or go back to change the selection.",
                explanation=f"The session was not started: {error}",
            )
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            return LaunchFailure(
                status=(
                    f"Attach with: {' '.join(self._services.attach_argv(str(record.session_id)))}"
                ),
                explanation=(
                    "The session did not become ready, but its pane may still exist. The "
                    "command below reaches it. Check this host before retrying, or a second "
                    "session will run alongside it."
                ),
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
