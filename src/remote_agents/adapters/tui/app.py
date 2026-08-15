"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TypeVar

from textual import events, work
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
from remote_agents.adapters.tui.screens.palette import NavigationCommands
from remote_agents.application.commands import (
    AnswerTrustCommand,
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
    StopFailure,
    available_actions,
    explain_state,
    remote_control_available,
    stop_failure,
)
from remote_agents.domain.conversations import ResolvedConversation
from remote_agents.domain.models import ProfileId, ProjectId, SessionRecord, SessionState
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TrustState

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
#: How long a failure toast stays up, against Textual's `NOTIFICATION_TIMEOUT` of 5. Long
#: enough to read the remedy at an unhurried pace rather than a skim, which is what the
#: default gave it — a gate evaluator measured the message at 55 words.
_FAILURE_TIMEOUT = 20.0


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
    ChoiceScreen #status { height: 2; padding: 0 1; text-overflow: ellipsis; color: $foreground; }
    /* Severity from the design system, so it resolves per theme rather than assuming a dark
       one — and always as the *second* signal: every caller that sets a severity has already
       said what went wrong in words, because a reader under NO_COLOR gets the words only. */
    ChoiceScreen #status.-error { color: $error; }
    ChoiceScreen #status.-warning { color: $warning; }
    ChoiceScreen OptionList { height: 1fr; }
    /* The empty-state row reads as an absence rather than as a choice. `$text-muted` from
       the design system, not a grey literal — and it is the *second* signal here too: the
       row is a disabled `Option`, so it is unselectable whatever the palette does. */
    ChoiceScreen OptionList > .option-list--option-disabled { color: $text-muted; }

    /* Which of the two bodies a screen shows is a *state*, declared once here, rather than
       four imperative `display =` assignments spread across `on_mount`, `show_output` and
       `hide_output`. Every screen starts on the list; a screen that has output adds the
       class and the pair swaps. */
    ChoiceScreen #output-pane { display: none; background: $surface; }
    ChoiceScreen.-showing-output #output-pane { display: block; }
    ChoiceScreen.-showing-output #choices { display: none; }
    ChoiceScreen #output { height: 1fr; padding: 0 1; border: none; background: $surface; }
    """
    # `border: none` on `#output` because it is a `TextArea` now, and `TextArea.DEFAULT_CSS`
    # draws `border: tall $border-blurred` — a box the `Static` it replaced never drew, which
    # would eat a row top and bottom of the pane and redraw itself on focus. `height: 1fr`
    # keeps it filling `#output-pane` exactly, so the container never scrolls on top of the
    # `TextArea`'s own scrolling.
    # `#status` is **two rows high and wraps**, and the difference between that and one row is
    # a defect a gate evaluator caught by driving the real thing at 80 columns. The contract is
    # unchanged — one *logical* line, enforced by `__init_subclass__`, the AST sweep over the
    # call sites, and `set_status`'s own runtime guard — and `height` is still fixed, so the
    # rows beneath it never move, which is the whole point of the region split. What one row
    # additionally imposed was a **display** limit nobody measured against the longest thing
    # this region carries: `Attach with: tmux -L remote-agents attach-session -t ra-<uuid>:` is
    # 93 characters, so at 80 columns it was ellipsised mid-UUID. That string is the one payload
    # here the owner has to *copy*, and a terminal can only copy what is drawn — so a cut one is
    # not a shortened command, it is no command, on the path where a session did not come up and
    # it is the only handle left on a pane that may still be live. Two rows hold it at 60.
    #: Shown in the header, with each screen's breadcrumb as the sub-title beside it. Set
    #: rather than left to default: `App.title` falls back to the class name, so the header
    #: read "RemoteAgentsTui" — the one string on screen that named an implementation detail.
    TITLE = "Remote Agents"
    #: The system commands stay — Textual's own (theme, quit, keys, maximize, screenshot) and
    #: none of them touches a session. `NavigationCommands` adds this app's three flow jumps
    #: and nothing else; DEC-007 is why, and `screens/palette.py` carries the argument.
    COMMANDS = App.COMMANDS | {NavigationCommands}
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
        Binding("ctrl+n", "add_project", "Add project", tooltip="Register a new project directory"),
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
        # Set once, never cleared: see `_leave`. Separate from `_busy` because the two answer
        # different questions and only one of them is temporary.
        self._leaving = False
        #: The (position, work) the quit warning was last given for — see `action_quit`. Keyed
        #: to the work so that typing more re-arms it rather than leaving on a stale yes.
        self._quit_armed: tuple[str, str] | None = None

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
        """Whether an action is mid-flight and no other may start.

        **Two reasons, not one, and the second was found by testing a queued keypress.** A
        command in flight is the temporary reason. A surface that has decided to leave is the
        permanent one: `exit()` does not tear the app down synchronously, so between the
        decision and the teardown the position is still mounted, still focused, and still has
        whatever the owner queued sitting in the pump behind the handler that just ran.

        Asked here rather than at each guard so the two cannot drift. Every consumer already
        routes through this property or `set_busy`, which is why folding the second reason in
        here fixes the handler guard (`ChoiceScreen.on_option_list_option_selected`), the
        auto-reload guard (`SessionsScreen._auto_reload`) and the app's own bindings at once,
        rather than in three places that would then each need their own version of it.
        """
        return self._busy or self._leaving

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def _leave(self, request: AttachRequest) -> None:
        """Exit the surface, and refuse everything from this moment on.

        **The flag is why this is not a one-liner in each of the two flows.**
        Both `launch` and `issue_resume` clear `_busy` in a `finally` and *then* exit, without
        leaving the position — so the second of two enters queued in one terminal read found
        the same screen, the same row and an open guard, and issued a second real command.
        Two managed sessions where one was asked for.

        Clearing the guard there is right: it is scoped to the awaited call, and the failure
        paths below it return to a screen that must be usable again. What was missing is that
        success does not return to anything, and until the app is actually gone the surface
        must stop answering. `_leaving` is therefore set and never cleared — there is no state
        after this one.

        DEC-008 is honoured rather than worked around: `exclusive` is still not passed
        anywhere, and nothing in flight is cancelled. The repeat is *dropped*, which is what
        that decision asks for; this only extends the window in which dropping happens to
        cover the gap between deciding to leave and being gone.
        """
        self._leaving = True
        self.exit(request)

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

        **A failure is given longer than Textual's five-second default**, because of what it
        carries. The stop-failure remedy is 55 words; five seconds is around 650 words a
        minute, so the half that says what to *do* was expiring before it could be read, and
        what it left behind was a status line naming the fault with no next step. An
        `information` toast is a confirmation of something that already happened and keeps the
        default. The window is still a window — anything the owner must keep belongs in the
        status line, which is the rule the attach command is already handled by.
        """
        timeout = _FAILURE_TIMEOUT if severity != "information" else None
        self.notify(message, severity=severity, markup=False, timeout=timeout)

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
        if self.busy:
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
        if self.busy:
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

    async def on_event(self, event: events.Event) -> None:
        """Disarm the quit warning on any key that is not another `ctrl+q`.

        **Without this the warning re-opens the defect it closes, and a Tier-1 review
        reproduced it.** `_quit_armed` was compared by value alone — position plus the text at
        risk — and nothing ever cleared it. So: type `orbit-relay`, press ctrl+q and be warned,
        decline, backspace the entry clean, retype the same name, press ctrl+q once — and the
        app left immediately, because the freshly computed signature happened to equal a
        signature armed against work that no longer existed. Correcting a typo back to its
        original value is an ordinary thing to do, and it silently discarded the work again.

        Keying the arm to the *work* was the right instinct and the wrong mechanism: a value
        cannot express "this same continuous stretch of typing". What actually needs to hold
        is that the owner has not changed the work since being warned, and that is a statement
        about *input*, not about text.

        **`Paste` as well as `Key`, and the first version of this missed it — a second
        Critical from the same review.** `events.Paste` is not an `events.Key`; it is not even
        an `events.InputEvent`, and `App.on_event` routes it down a separate branch. So
        `Input._on_paste` can replace a mouse-made selection with identical clipboard text
        while emitting no key at all, leaving the arm standing over work that was destroyed
        and rebuilt — the same defect as the retype case, through a path the key check could
        not see. Re-pasting a name to confirm it is an ordinary thing to do.

        **`MouseEvent` is deliberately excluded**, which is why this names two classes instead
        of taking `events.InputEvent`. That base is `Key` and `MouseEvent` only — `Paste` is
        not under it, so it would not have helped — and it would disarm on every mouse *move*.
        A terminal reporting motion would then re-warn on every second press, which is a
        refusal wearing a warning's clothes: the one outcome worse than the bug being fixed.
        A click can move a cursor; it cannot change what is at risk, and a change by any other
        route still moves the signature.

        One override rather than a disarm in each of the five actions, which is the
        arrangement `action_quit`'s own docstring argues against for the same reason: five
        sites is five chances to forget, and the sixth action added later would forget.
        """
        if isinstance(event, events.Paste) or (
            isinstance(event, events.Key) and event.key != "ctrl+q"
        ):
            self._quit_armed = None
        await super().on_event(event)

    async def action_quit(self) -> None:
        """Leave — but say what is about to be lost first, and only ask once.

        `ctrl+q` used to discard typed work silently, and it was reproduced: type a
        label, press it, the app is gone and the label with it. `screens/base.py` deliberately
        left quit out of the set of keys greyed while `work_in_flight`, and that reasoning
        stands — a jump means "go elsewhere in this app" and losing the work is a side effect
        nobody asked for, while quit means "leave". **The fix is a warning, never a refusal.**
        An app that will not close until an entry is cleared is a worse answer than the silent
        discard it replaces, so the second press always leaves.

        **Not a modal, and DEC-025 is why.** The obvious implementation is `ask_to_confirm`,
        as the force-stop and Remote Control confirmations do it. That decision forbids
        exactly this caller: a confirmation may only be asked from a screen's own handler,
        and it names "a global binding" among the callers it exists to warn off. The
        protection those confirmations rely on is that a screen handler holds the pump while
        it waits, so nothing can pop the modal out from under the await. A global binding
        holds no such thing, and an unanswered `push_screen_wait` neither returns nor raises.
        So the question is asked on the key itself: nothing suspends, and nothing can hang.

        **Armed against the work, not against a clock or a bare flag.** What is remembered is
        the position and the text that was at risk when the warning was given. Keep typing and
        that signature moves, so the next press warns again about the *new* work rather than
        leaving on a yes that answered a smaller question. A bare flag would have had to be
        cleared by every other action to get the same property — five call sites today and a
        sixth one forgotten later.

        Owner text reaches the toast through `announce`, which is the one place `markup=False`
        is decided (DEC-014); a label containing an unbalanced bracket is a rendering fault
        this surface has already paid for once.
        """
        screen = self.screen if self.screen_stack else None
        if isinstance(screen, ChoiceScreen) and screen.work_in_flight:
            at_risk = screen.work_at_risk
            signature = (screen.position, at_risk)
            if self._quit_armed != signature:
                self._quit_armed = signature
                screen.announce(
                    (
                        f"Quitting now discards {at_risk!r}, which has not been saved. "
                        "Press ctrl+q again to leave anyway."
                    )
                    if at_risk
                    else (
                        "Quitting now discards what you have built on this screen. "
                        "Press ctrl+q again to leave anyway."
                    ),
                    severity="warning",
                )
                return
        await super().action_quit()

    async def action_add_project(self) -> None:
        if not self.busy:
            await self.show_areas()

    async def show_areas(self) -> None:
        await self.switch_flow(AreasScreen())

    async def action_sessions(self) -> None:
        """Show every managed session, including ones this process never launched."""
        if self.busy:
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
        if self.busy or self._services.conversations is None:
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
            async with screen.awaiting("Resuming the conversation…"):
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
        self._leave(AttachRequest(session_id, self._services.attach_argv(session_id)))

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
    # **Every branch where that re-read disagrees with the rows redraws before returning**, and
    # all eight of them go through `ChoiceScreen.refuse` rather than saying so individually. A
    # re-read that finds the session gone, or in a state the policy no longer offers this action
    # for, has established that the rows in front of the owner are wrong — they were built from
    # the earlier read. Reporting and returning left them there, still offering a stop for a
    # session that had ended. Found as a class after a Tier-1 review named two of the branches,
    # and extracted after the stage's Tier-2 review found the repair written out by hand at
    # every one of them.

    async def answer_trust(self, record, screen) -> TrustState | None:
        """Answer the folder-trust question for `record`, or report why it did not happen.

        Returns None when nothing was issued -- the screen has already been told why -- so
        the caller can tell "did not run" from "ran and the pane is still asking", which are
        different things to say to the owner.
        """
        try:
            return await self._services.launcher.answer_trust(
                AnswerTrustCommand(record.session_id, _idempotency_key())
            )
        except Exception as error:
            _LOG.exception("answering the trust question failed")
            screen.set_status(f"The trust answer did not go through: {error}")
            return None

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
                await screen.refuse()
                return
            if not remote_control_available(record):
                await screen.refuse("Remote Control is no longer available for this session.")
                return
            async with screen.awaiting(f"Setting Remote Control to {desired.value}…"):
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
        if self.busy:
            return
        self._busy = True
        try:
            record = await self.current_record(session_value)
            if record is None:
                await screen.refuse()
                return
            if action not in available_actions(record.state):
                await screen.refuse(
                    f"{_ACTION_LABELS[action]} is no longer available for this session. "
                    f"{explain_state(record.state)}"
                )
                return
            async with screen.awaiting(f"{_ACTION_LABELS[action]}…"):
                failure = await self._issue_stop(action, record)
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
            if failure is None:
                # Said at all, because the bot has always said it and this surface never did.
                # A graceful stop that works ends the session, so the redraw above replaces the
                # detail with "That session is no longer available." — true, and identical to
                # what the owner would see if the stop had never been issued. Telegram sends
                # "Stopped <session>"; DEC-007 wants the two to agree about what a stop did,
                # and agreeing about the failures while disagreeing about the successes is
                # half of a shared vocabulary. Found by the Stage 2 gate evaluator.
                screen.announce(
                    f"{_ACTION_LABELS[action]}: the session has ended.", severity="information"
                )
            if failure is not None:
                # After the re-read, not before it, for the reason `set_remote_control` gives:
                # `after_command` rewrites the status from the store, so a result painted
                # first is immediately overwritten by it.
                #
                # And the re-read is what makes this necessary rather than merely useful. A
                # graceful stop that did not take effect leaves the session RUNNING, so the
                # refreshed detail says "State: running. The agent is running." — true, and
                # indistinguishable from the same screen before the stop was ever asked for.
                # That sentence *is* BL-008: the surface had no way to say that something was
                # attempted and did not happen.
                screen.set_status(
                    f"{_ACTION_LABELS[action]} did not take effect. {failure.summary}"
                )
                # The summary opens the notification too, rather than the remedy alone. A
                # toast is read on its own and gone a few seconds later, so one that starts
                # mid-explanation asks the owner to have been looking at the status line at
                # the right moment. The overlap between the two is the point, not an oversight.
                screen.announce(f"{failure.summary} {failure.remedy}")
        finally:
            self._busy = False

    async def _issue_stop(self, action: str, record: SessionRecord) -> StopFailure | None:
        """Send exactly one curated command, and answer why it did not take effect.

        Force is its own named branch and an unrecognized action raises, rather than the kill
        being the trailing `else`. It was, and nothing could reach it — `stop` only calls this
        for an action `available_actions` returned. But "anything I do not recognize is a
        kill" is a fail-dangerous default in the one method that kills, and the cost of it
        being right is that every future caller stays correct by accident.

        **Only `graceful_stop` answers anything, and that is BL-008's scope rather than the
        whole of the problem.** Its `TerminalObservation` has always distinguished a clean exit
        from a timeout — the service's own docstring says `preserved` "remains the way a caller
        tells a clean exit from `graceful_timeout`" — and both surfaces threw the value away.
        `cleanup` returns nothing at all, so there is nothing there to read.

        **`force_stop` is a different matter and this docstring used to misdescribe it.** It
        said force's observation "describes a kill the state machine has already recorded",
        implying there was nothing to distinguish. There is: `TmuxRuntime.force_stop` returns
        `detail="ownership_lost"` *without* killing anything when no managed pane matches, and
        `SessionService.force_stop` records `VERIFIED_FORCE_STOP` regardless, so both surfaces
        report "the session has ended" over an agent that may still be running. That is the
        same two-causes-that-read-alike shape BL-008 names, in the one method that kills.

        It is **not fixed here**, and the reason is a boundary rather than an oversight: the
        honest repair is for the service to stop recording a kill it did not perform, which is
        an application-layer behaviour change on the destructive path — the kind DEC-006 was
        recorded for — and it needs an owner's decision about whether force should fail closed,
        not a presentation edit. Recorded as BL-026. Found by the Stage 2 gate's second review
        pass, checking this docstring's own claim against the runtime.
        """
        launcher = self._services.launcher
        if action == GRACEFUL:
            observation = await launcher.graceful_stop(
                GracefulStopCommand(record.session_id, record.profile_id)
            )
            return stop_failure(observation)
        if action == CLEANUP:
            await launcher.cleanup(CleanupCommand(record.session_id))
            return None
        if action == FORCE:
            await launcher.force_stop(ForceStopCommand(record.session_id))
            return None
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
        # **The status states the failure, it does not merely point at the exit.** It read
        # "Press escape to return to the project list." — a sentence that reports nothing —
        # while the *why* went to a toast that expires after 20 seconds. A gate evaluator
        # drove this and found that once the toast had gone, an unreadable store was
        # distinguishable from an ordinary empty list only by the *absence* of the empty-state
        # row. The two better-behaved paths in this surface already do it this way: a launch
        # that produced nothing leaves "Nothing was started." on screen.
        #
        # The severity is honest here for the same reason it is refused on a bare navigation
        # instruction: these words name the condition, so an owner under NO_COLOR reads it
        # from the sentence and the colour only makes it quicker to find.
        target.set_status(
            "The managed sessions could not be read. Press escape to return to the project list.",
            severity="error",
        )
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
        # `body.set_status("Launching…")` used to stand here on its own, and it was the only
        # sign any of the five awaited flows gave that it was working — a static string in a
        # region the owner has no reason to be watching, while the rows under the cursor stayed
        # live and stale. It is now what `awaiting` puts there for the duration and takes back
        # afterwards, which is the same message with the two things it was missing: the rows are
        # covered while it shows, and the other four flows say their own version of it.
        body = self.body
        try:
            # `nullcontext` when there is no body to cover. **Not reachable today**, and worth
            # saying so rather than inventing a scenario: the only caller is
            # `ReviewScreen.choose`, reached from a row selection on that very screen, and
            # `self.body` is read synchronously before any await. Kept because `self.body`
            # answers `None` for reasons that have nothing to do with this call site — it is the
            # unchecked `cast` BL-021 recorded — so a second caller would get the honest answer
            # rather than an `AttributeError`.
            covered = body.awaiting("Launching…") if body is not None else contextlib.nullcontext()
            async with covered:
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
        self._leave(AttachRequest(session_id, self._services.attach_argv(session_id)))
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
