"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TypeVar

from textual import events, work
from textual.app import App, ScreenStackError
from textual.binding import Binding
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.worker import WorkerCancelled, WorkerFailed

from remote_agents.adapters.tui.context import TuiContext
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
from remote_agents.adapters.tui.preferences import (
    ALPHABETICAL,
    RECENCY,
    read_project_order,
    write_project_order,
)
from remote_agents.adapters.tui.screens import (
    ALL_SCREENS,
    AreasScreen,
    DashboardScreen,
    OpeningAction,
    ResumeProjectsScreen,
    SessionDetailScreen,
    SessionsScreen,
)
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import ConfirmScreen
from remote_agents.adapters.tui.screens.launch import ProjectsScreen
from remote_agents.adapters.tui.screens.palette import NavigationCommands
from remote_agents.application.commands import (
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import (
    CatalogProject,
    order_alphabetically,
    rank_if_usage_is_reported,
)
from remote_agents.application.session_actions import (
    ACTION_LABELS as _ACTION_LABELS,
)
from remote_agents.application.session_actions import (
    FORCE,
    explain_state,
    remote_control_available,
)
from remote_agents.application.session_views import (
    context_gauge,
    listed_sessions,
    only_listed,
    with_project_names,
)
from remote_agents.application.stops import dispatch_stop, resolve_stop
from remote_agents.domain.conversations import ResolvedConversation
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.agent_usage import ContextWindow

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
    "ProfileAvailability",
    "RemoteAgentsTui",
    "TuiContext",
    "age",
    "conversation_row",
    "label_or_error",
    "run_local_terminal",
    "selectable_area",
    "session_row",
]


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
    /* Three rows on the sessions positions alone, because they are the only ones carrying a
       whole keymap in this region: seven row keys, two navigation keys and the count. Measured
       at 60 columns before the rule was written — two rows hold about 114 characters and drop
       the remainder with no ellipsis at all, so the previous 112-character pane status was one
       key away from losing "m remote" with nothing on screen to say it had. Three rows hold
       about 171. The type selector matches `SessionsPaneScreen` too, Textual's type names
       including base classes; the pane pays one row of its list for it. */
    SessionsScreen #status { height: 3; }
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
    #
    # **It is no longer the longest, and the margin is narrower than the paragraph above
    # implies.** The conversation list's status — which names the terminal handover as well as
    # the page — is 109 characters, measured. Two rows hold 116 at 60 columns, so it still fits
    # with about seven characters of slack and clips at roughly 50. The design point stands; what
    # changed is that the worked example is no longer the worst case, and a future line longer
    # than this one has less room than the 93-character figure suggests.
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

    #: Which app-level flows this surface offers, by action name.
    #:
    #: The combined dashboard offers all three, and did so when it was the only surface.
    #: A console *pane* is a process of its own that inherits these bindings, so without this
    #: the feed pane advertised and honoured "Add project" — pushing the project wizard into
    #: the notifications pane, where escape returns to a feed. Carried from the Stage 1 gate
    #: to the key budget task and answered here: a pane offers the flows it owns.
    #:
    #: Read by `offers`, which both `check_action` and each action consult — one predicate,
    #: because `ChoiceScreen.check_action`'s own rule is that a footer entry may only be
    #: hidden where the action it names already declines to run.
    flows: frozenset[str] = frozenset({"add_project", "resume", "sessions"})

    def offers(self, action: str) -> bool:
        """Whether this surface offers one of the app-level flows."""
        return action in self.flows

    def __init__(self, context: TuiContext) -> None:
        super().__init__()
        self._services = context
        #: The catalogue exactly as it was read, before either order is applied. Held apart
        #: from `_catalogue` so a switch re-orders the *snapshot* rather than the list it is
        #: looking at: `rank_by_recent_use` is a stable sort, so ranking an already
        #: alphabetical list would silently take alphabetical as the tie-break and lose
        #: DEC-012's registered-first-then-alphabetical fallback after one round trip.
        self._raw_catalogue = context.backend.catalogue
        self._context_windows: dict[str, ContextWindow] = {}
        """The last context reading per session, keyed by session id as a string.

        A cache, and having one is the whole of this feature's cost control. The sessions pane
        repaints every ten seconds; a provider read per row on that tick would put a directory
        sweep and a tail read *per session* behind a timer nobody asked to start. So the rows
        render from whatever is here and the reads happen on their own slower schedule --
        `refresh_context_windows`, which no repaint calls.

        Held on the app rather than on a screen because two screens draw these rows (the
        dashboard's pane and the sessions list), and a cache per screen would read the same
        files twice and let the two disagree about one session.
        """
        self._catalogue = context.backend.catalogue
        #: Which of the two orders the projects list is in. Read once, from the file the
        #: composition root pointed at, and total in every failure -- an unreadable
        #: preference is a forgotten choice, never a surface that will not start.
        self._project_order = read_project_order(context.preferences_path)
        #: Whether `_catalogue` has had that order applied. The snapshot arrives from
        #: `Backend.catalogue` in whatever order `build_catalogue` produced, and ordering it
        #: needs the store, which cannot be read from a synchronous constructor.
        self._catalogue_ordered = False
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
        # The surface's own answer comes first: a flow this pane does not offer is hidden
        # wherever it is, and the action that names it declines to run for the same reason.
        if action in {"add_project", "resume", "sessions"} and not self.offers(action):
            return False
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

        The dashboard subclasses the projects picker, so `return_to_projects`'s isinstance
        check and every flow that unwinds to "the projects position" land here unchanged.
        """
        return DashboardScreen()

    # Shared state screens read ---------------------------------------------------

    @property
    def services(self) -> TuiContext:
        return self._services

    @property
    def catalogue(self) -> tuple[CatalogProject, ...]:
        return self._catalogue

    @property
    def project_order(self) -> str:
        """Which order the projects list is drawn in — one of `preferences.PROJECT_ORDERS`."""
        return self._project_order

    async def ensure_catalogue_ordered(self) -> None:
        """Apply the chosen order to the snapshot this process started with, exactly once.

        **Why this is not done in `__init__`, and not in the app's `on_mount` either.**
        Ranking by recent use has to read the store, which is async, so a synchronous
        constructor cannot do it. `on_mount` can, but the app's Mount is dispatched at the
        same moment the default screen's own pump is started (`App._process_messages`), so
        the first `await` here would hand control to a screen that then draws the *unordered*
        snapshot — the first draw and every later draw disagreeing, which is exactly the
        defect this task exists to close. Awaited from `ProjectsScreen.populate` instead,
        which `ChoiceScreen.on_mount` awaits *before* anything is rendered.

        Idempotent, because it is reached from every screen that rests on the projects
        position and a re-order per mount would be a re-order per render in disguise
        (DEC-012: once per catalogue refresh).
        """
        if self._catalogue_ordered:
            return
        # Set **before** the await, deliberately, and unlike `switch_project_order` and
        # `reload_catalogue`, which set it after. Those two are recording a fact; this one is
        # a re-entrancy guard, and a guard raised after the suspension point does not guard --
        # two screens mounting while the store read is in flight would both pass the check and
        # both rank. The cost is a window where the flag reads True over an unordered
        # catalogue -- nothing in this app's screen-stack model can observe it today, because
        # no path mounts two `ProjectsScreen`-family screens concurrently, but that is a fact
        # about the current navigation model rather than something this guard enforces. A pane
        # type or a background screen that did populate concurrently would have to re-derive
        # this trade rather than inherit it.
        self._catalogue_ordered = True
        self._catalogue = await self._ordered(self._raw_catalogue)

    async def switch_project_order(self) -> str:
        """Move to the other order, record the choice, and answer which one is now in force.

        On the app because the catalogue and the chosen order are both app state, while the
        caller is a screen -- the same split `reload_catalogue` is on, and for the same
        reason: the sentence that reports it is the screen's.

        The write is total (`adapters/tui/preferences.py`) and the path may be absent, so a
        host that wired no preferences file switches exactly like one that did and forgets
        between runs. Re-orders the *unordered* snapshot, never the drawn list.
        """
        self._project_order = RECENCY if self._project_order == ALPHABETICAL else ALPHABETICAL
        write_project_order(self._services.preferences_path, self._project_order)
        self._catalogue = await self._ordered(self._raw_catalogue)
        self._catalogue_ordered = True
        return self._project_order

    async def _ordered(self, catalogue: tuple[CatalogProject, ...]) -> tuple[CatalogProject, ...]:
        """The one place either order is applied, so the two draw paths cannot disagree.

        `now` is read here, at the one caller with a reason to know the time, and both
        orderings behind it stay pure (`application/project_catalog.py`). A read that fails
        leaves the catalogue in the order it came: the list is then unranked rather than
        absent, which is the same answer a host with no launch history gets.
        """
        try:
            if self._project_order == ALPHABETICAL:
                return order_alphabetically(catalogue)
            return await rank_if_usage_is_reported(
                catalogue, self._services.backend.sessions, datetime.now(UTC)
            )
        except Exception:
            _LOG.exception("ordering the project catalogue failed")
            return catalogue

    @property
    def busy(self) -> bool:
        """Whether an action is mid-flight and no other may start.

        **Two reasons, not one, and the second was found by testing a queued keypress.** A
        command in flight is the temporary reason. A surface that has decided to leave is the
        permanent one: `exit()` does not tear the app down synchronously, so between the
        decision and the teardown the position is still mounted, still focused, and still has
        whatever the owner queued sitting in the pump behind the handler that just ran.

        Asked here rather than at each guard so the two cannot drift. Folding the second
        reason in here covers the handler guard (`ChoiceScreen.on_option_list_option_selected`),
        the auto-reload guard (`SessionsScreen._auto_reload`) and the five app-level actions at
        once, rather than in three places that would then each need their own version of it.

        **`action_quit` is the exception and reads `_leaving` directly**, which is worth
        stating because an earlier version of this paragraph claimed every consumer routes
        through here and a stage review caught that it did not. Quit is deliberately exempt
        from `check_action`'s rules — an app that cannot be closed is worse than one that
        loses work — so it cannot take the broad `busy` answer without reintroducing exactly
        that complaint. It refuses only in the narrower leaving window, and its own docstring
        carries the reason.
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

    async def _open_or_leave(self, session_id: str) -> None:
        """Open one ready session by the route the composition wired, in one place.

        `launch` and `issue_resume` both end here so they cannot each invent their own
        answer to "does opening a session end the surface". Without the console capability
        this is exactly the old contract: exit with an `AttachRequest` and let `attach_to`
        exec. With it, the client is switched to the session and the surface stays alive —
        returned to its resting screen, so the jump-home key finds it at rest rather than
        mid-flow.
        """
        opener = self._services.open_in_console
        if opener is None:
            self._leave(AttachRequest(session_id, self._services.attach_argv(session_id)))
            return
        # Re-arm the guard for the switch's own await. Both callers clear `_busy` in their
        # `finally` just before calling here, and on the exec route `_leave`'s permanent
        # latch covers the gap — but this route succeeds *without* leaving, so without this
        # the queued second Enter `_leave`'s docstring describes (DEC-008's defect) would
        # find an open guard mid-switch and issue a second real launch. There is no yield
        # between the caller's clear and this set, so the window never opens.
        self._busy = True
        try:
            refused = await opener(session_id)
        except Exception as error:
            _LOG.exception("switching the client to the session failed")
            self.announce(f"The session is running but could not be opened: {error}")
            return
        finally:
            self._busy = False
        if refused is not None:
            # The console declined, and said why. It degrades to a log line by contract and
            # nothing configures logging, so without this the owner presses enter on a row
            # and watches nothing happen — which is exactly what shipped.
            self.announce(refused, severity="warning")
            return
        # "the projects position" is this *app's* resting position, whatever that is: the
        # method unwinds to stack depth 1 and only re-renders when what it finds is a
        # projects screen. On the console's sessions and feed panes the resting position is
        # the pane itself, so this unwinds any pushed flow and leaves the pane in front —
        # which is what "the pane the owner opened from stays visible" means there.
        self.return_to_projects()

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
            read = await self.in_thread(self._services.backend.refresh_catalogue, group="catalogue")
        except Exception:
            _LOG.exception("catalogue refresh failed")
            return False
        # A refresh is exactly where a new order is expected, and it is one of the three
        # places an order is computed at all -- the other two being the first draw and the
        # owner pressing the key (DEC-012: never per render). Marked ordered so a screen
        # mounting afterwards does not rank the same snapshot a second time.
        self._raw_catalogue = read
        self._catalogue = await self._ordered(read)
        self._catalogue_ordered = True
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

        `ctrl+q` used to discard typed work silently, and it was reproduced on the launch
        wizard's label entry, since removed: type a name, press it, the app is gone and the
        name with it. The project-name entry reproduces it the same way today.
        `screens/base.py` deliberately
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
        if self._leaving:
            # **The one rule quit does obey, and it is not `busy`.** `check_action` exempts
            # quit from every other rule on the argument that an app which cannot be closed is
            # worse than one that loses work — and that argument is right, but it predates
            # `_leaving` and does not reach this case. Here the surface has *already* decided
            # to leave and is carrying the attach request the launch or resume just produced;
            # `App.exit()` overwrites `_return_value` unconditionally and `App.action_quit`
            # calls it with no argument, so answering this key would replace that request with
            # `None`. The session would keep running with nothing attaching to it.
            #
            # Not `self.busy`: that would also refuse quit while an ordinary command is in
            # flight, which is exactly the "cannot be closed" complaint. `_leaving` is the
            # narrower claim — the app is already going, so the key has nothing left to do.
            #
            # Found by the Stage 2 gate's Tier-2 pass, the first review to see this task and
            # `_leave` together.
            return
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
        if self.offers("add_project") and not self.busy:
            await self.show_areas()

    async def show_areas(self) -> None:
        await self.switch_flow(AreasScreen())

    async def action_sessions(self) -> None:
        """Show every managed session, including ones this process never launched."""
        if self.busy or not self.offers("sessions"):
            return
        await self.show_sessions()

    async def show_sessions(self) -> None:
        screen = self.screen
        if isinstance(screen, SessionsScreen):
            await screen.reload()
            return
        await self.switch_flow(SessionsScreen())

    async def show_detail(self, session_value: str, opening_action: str | None = None) -> None:
        """Open — or redraw — the detail for one session, optionally performing one action.

        Redrawing rather than pushing when the same detail is already on screen is what keeps
        a stop, a confirmation abort, or a remote-control change from growing the stack by one
        screen every time the owner uses it.

        `opening_action` is what the sessions pane's per-action keys carry. On the redraw path
        it is dispatched explicitly, because that path does not re-mount the screen and so
        never reaches `populate` — a key pressed on a detail already showing must still do
        what it says. It is passed through `choose` there for the same reason `populate` does:
        one chain, with all of its guards.
        """
        screen = self.screen
        if isinstance(screen, SessionDetailScreen) and screen.session_value == session_value:
            await screen.render_detail()
            if opening_action is not None:
                # Posted, not awaited, so this path is byte-for-byte the mount path's:
                # both reach `dispatch_opening` from `on_opening_action`, a screen handler on
                # the screen's own pump -- which is where a pressed row reaches `choose` from
                # too, and is what DEC-025 requires. Awaiting it here instead ran the
                # confirmation from whatever coroutine happened to call `show_detail`, which
                # is a second way to raise a modal and hung outright when that caller was not
                # the pump.
                screen.post_message(OpeningAction(opening_action))
            return
        await self.push_screen(SessionDetailScreen(session_value, opening_action))

    # Resume ---------------------------------------------------------------------

    async def action_resume(self) -> None:
        """Open the resume flow, if this host wired a conversation service at all."""
        if self.busy or not self.offers("resume") or self._services.backend.conversations is None:
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
                outcome = await self._services.backend.sessions.resume(
                    ResumeCommand(
                        ProjectId(project.opaque_id),
                        ProfileId(profile),
                        resolved,
                        _idempotency_key(),
                    )
                )
            # `outcome.created` says whether this call started anything, which the record
            # cannot: an already-bound RUNNING session and a fresh resume are the same state.
            # This surface does not branch on it, and that is deliberate rather than an
            # oversight — it *attaches*, and attaching to the session a conversation is
            # already bound to is the right destination either way. The bot needs the bit
            # because it prints a sentence, and printing "Session resumed" over a session it
            # had not touched was the defect. Recorded here so the asymmetry reads as a
            # decision the next person can check rather than as one surface being behind.
            record = outcome.record
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
        await self._open_or_leave(str(record.session_id))

    # The mutating path ------------------------------------------------------------
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

    async def set_remote_control(
        self, session_value: str, desired: RemoteControlState, screen: ChoiceScreen
    ) -> None:
        """Change one session's control mode, after re-reading and re-checking the policy.

        This *used* to open without `if self.busy: return`, and the docstring here explained
        why: what refused a
        second concurrent change was `ChoiceScreen.on_option_list_option_selected`, which drops
        a row selection while the surface is busy, so the refusal happened before this was
        ever called. It then said, of the single caller that arrangement depended on, that "a
        second caller reaching this directly would not be refused here, which is the thing to
        check before adding one."

        Stage 4 added exactly that second caller. `SessionDetailScreen.dispatch_opening`
        repeats the busy check, which closes it — but a rule that every *entry* must remember
        is a rule a third entry can forget, and the note above was already the record of
        someone foreseeing that and it happening anyway. So the guard moved to where the
        command is issued, which is the one place no caller can route around, and this is now
        symmetric with `stop`.

        Safe to add precisely because `confirm_remote_control` calls this *outside* its own
        `holding_the_guard()` block — the same shape `confirm_force` uses for `stop`, and the
        reason `stop` could always afford this guard.
        """
        if self.busy:
            return
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
                state = await self._services.backend.sessions.set_remote_control(
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
            try:
                expected = SessionId.parse(session_value)
            except ValueError:
                # `current_record` used to answer this case by simply matching nothing, and
                # the refusal below is the same one it produced. Kept explicit because the
                # shared use case takes a parsed id, so an unparseable value would otherwise
                # raise into the handler underneath and report "did not complete" over a
                # session that was never identified — a different sentence for the same
                # nothing-happened.
                await screen.refuse()
                return
            # Two calls rather than one, because the spinner belongs around the dispatch and
            # not around the re-read: `awaiting` covers the rows and rewrites the status line,
            # and its own docstring scopes it to "the part where something outside this
            # process has been asked and has not replied". `execute_stop` is exactly these
            # two, and the bot uses it because it has nothing to do in between.
            resolution = await resolve_stop(
                action, expected, read_record=lambda: self.current_record(session_value)
            )
            record = resolution.record
            if record is None:
                await screen.refuse()
                return
            if resolution.refusal is not None:
                # Every refusal that reaches here with a record is a policy refusal, and this
                # sentence is written for that one. `IDENTITY` would be the wrong sentence —
                # the action did not become unavailable, a different record came back than
                # the one asked about — and it is unreachable rather than handled: this
                # surface passes no `profile_id`, and `current_record` only ever returns a
                # record whose id already equals `session_value`, which `expected` parsed
                # from. **Loosening `current_record`'s matching, or passing a `profile_id`
                # here, makes it reachable and this line wrong**, which is the whole reason
                # the assumption is written down rather than left implicit across two files.
                await screen.refuse(
                    f"{_ACTION_LABELS[action]} is no longer available for this session. "
                    f"{explain_state(record.state, record.orphan_provenance)}"
                )
                return
            async with screen.awaiting(f"{_ACTION_LABELS[action]}…"):
                outcome = await dispatch_stop(resolution, sessions=self._services.backend.sessions)
            failure = outcome.failure
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
                #
                # **Force gets a different opening, because "did not take effect" would be
                # false for it (DEC-017).** A force that found no pane still ends the
                # session and still clears the row — that is the decision, taken because a row
                # the owner cannot clear is worse than an over-confident message. So the stop
                # very much took effect on the record; what it did not do is the kill it used
                # to claim. Reporting it as a failed stop would trade one wrong claim for
                # another, and would tell the owner to retry something that already happened.
                opening = (
                    f"{_ACTION_LABELS[action]}: the session has ended."
                    if action == FORCE
                    else f"{_ACTION_LABELS[action]} did not take effect."
                )
                screen.set_status(f"{opening} {failure.summary}")
                # The summary opens the notification too, rather than the remedy alone. A
                # toast is read on its own and gone a few seconds later, so one that starts
                # mid-explanation asks the owner to have been looking at the status line at
                # the right moment. The overlap between the two is the point, not an oversight.
                screen.announce(f"{failure.summary} {failure.remedy}")
        finally:
            self._busy = False

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
        # The Back row is offered only where backing out is a thing that happens. On a screen
        # that is its own process's resting position — every console pane — `go_back` refuses
        # to pop, so the row is a key that does nothing, drawn at the moment the owner most
        # needs the screen to be honest.
        target.show_choices(((_BACK, "Back"),) if len(self.screen_stack) > 1 else ())
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
        route = target.read_failure_route
        target.set_status(
            f"The managed sessions could not be read. {route}",
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

    def _with_names(self, records: tuple[SessionRecord, ...]) -> tuple[SessionRecord, ...]:
        """Join this surface's catalogue onto the records, by the shared rule.

        **Here rather than in the three renders that show a row, and that is the whole of
        this task's deviation from the plan's `Scope:` line.** The plan named
        `SessionsScreen._draw_listing`, `DashboardScreen._reload_sessions_pane` and
        `SessionDetailScreen.render_detail` -- three sites, and it noted that the first of
        them has two branches which "must name identically". They do not have to, because
        they no longer decide: both reads this surface performs pass through here, so a
        render cannot draw an unnamed record without first getting one from somewhere that
        does not exist. Three call sites that must agree, replaced by one they all read from,
        is the same argument Task 1.1 made one layer up -- applying it here and not there
        would have been holding the argument and declining to use it.

        The rule itself is `application/session_views.with_project_names`; what is this
        surface's own is which catalogue, and that is `self._catalogue` -- the local
        catalogue, refreshed by `reload_catalogue`.
        """
        return with_project_names(records, self._catalogue)

    def context_gauge_for(self, session_id: object) -> str:
        """The gauge suffix for one row, or nothing at all when there is no reading.

        Nothing, deliberately: an absent reading is not a zero one, and a row is not the place
        to explain the difference -- the session detail already words both absences. A row that
        rendered an empty bar for an unread session would assert a fullness nobody measured.
        """
        window = self._context_windows.get(str(session_id))
        return "" if window is None else f"  {context_gauge(window)}"

    async def refresh_context_windows(self, records: Iterable[SessionRecord]) -> None:
        """Re-read every listed session's context, off the repaint path.

        Called on its own slower schedule, never from a draw: see `_context_windows`. Total by
        construction like every other read into this surface -- a provider that changed its
        file format under an upgrade costs the gauges and never the list.

        Rebuilt rather than updated, so a session that has ended stops carrying a reading and a
        read that failed shows no gauge rather than the previous one. A context window is not a
        rate-limit percentage -- it belongs to a conversation that is still there or is not --
        but the same rule applies for the same reason: what is drawn should be something that
        was read, not something that was read once.
        """
        reader = self._services.backend.usage
        if reader is None:
            return
        fresh: dict[str, ContextWindow] = {}
        for record in records:
            try:
                usage = await reader(record.session_id)
            except Exception:
                _LOG.debug("a session usage read failed", exc_info=True)
                continue
            if usage is not None and usage.context is not None:
                fresh[str(record.session_id)] = usage.context
        self._context_windows = fresh

    async def load_sessions(self) -> tuple[SessionRecord, ...]:
        """Refresh readiness, then return the sessions worth showing.

        Order is whatever the store returns; nothing here sorts, and the row's age column
        is what tells the owner how old a session is.

        Console-hosted, this is also where the console catches up with the *other* writer.
        The bot now steps the console aside itself before a stop destroys a pane — it builds
        a composer for that one operation (DEC-005, and `bootstrap._private_boundary`) — so
        this is no longer the only thing standing between a remote stop and a console short
        a pane. What still arrives here is what neither writer's `hide` covered: a hide that
        hit its 2s cap, a console too degraded to arrange, or a session that ended without
        either surface asking. Those go unnoticed until something reads the list — this
        method's reveal, Ctrl+R, or the 10s auto-refresh — and then the projects surface is
        put back. That is a stated latency, not an accident: this is deliberately the only
        sync schedule there is. A stop issued from *this* surface does not wait for it; the
        stop paths step the console aside before destroying a pane. The sync degrades to
        nothing on failure by its own contract; a broken console never costs this list.

        It used to reconcile a tab per live session, which is what "sync" named. That
        mechanism retired with Sub-plan 3's Task 2.4.
        """
        records = await listed_sessions(self._services.backend.sessions)
        if self._services.console_sync is not None:
            await self._services.console_sync(records)
        # Named *after* the sync, deliberately. The sync matches records against tmux panes,
        # and while naming touches only `display.project_slug` and no id, handing it the
        # exact tuple it saw before this task removes the question entirely.
        return self._with_names(records)

    async def raw_sessions(self) -> tuple[SessionRecord, ...]:
        """Every session the store holds, named, and filtered by nothing.

        The feed needs this and the lists must not have it. A notification outlives its
        session by design, so naming a feed row routinely needs a record `only_listed` has
        correctly removed (DEC-017's "exactly ENDED") -- while a *list* showing that same
        record would be offering the owner a session with nothing left to reach.

        Here rather than in `screens/feed.py`, which had built its own `list_sessions()` plus
        `with_project_names(records, catalogue)` -- a second copy of the join `_with_names`
        already is, in the stage whose whole subject was that the join has one home.
        """
        return self._with_names(await self._services.backend.sessions.list_sessions())

    async def read_sessions(self) -> tuple[SessionRecord, ...]:
        """List the store's sessions, filtering what no surface can act on."""
        # The comment this replaces claimed the filter matched the bot's "exactly", which was
        # true and checked by nothing — the two were separate generator expressions over the
        # same enum. `only_listed` is now the one of them, and DEC-017's "exactly ENDED" is
        # asserted over the whole `SessionState` set rather than over the states a reader
        # thought to name.
        return only_listed(await self.raw_sessions())

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
                record = await self._services.backend.sessions.launch(
                    LaunchCommand(
                        ProjectId(project.opaque_id),
                        ProfileId(profile.profile_id),
                        _idempotency_key(),
                        # No label at launch, which is now what both surfaces do. Naming a
                        # session happens on the session, from the detail's Rename row, at the
                        # moment the owner can see what they are naming.
                        None,
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
        await self._open_or_leave(str(record.session_id))
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
