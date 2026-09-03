"""The resting position: projects on the left, sessions and notifications on the right.

Two screens live here. `ProjectsPaneScreen` is the projects position with the Launch-or-Resume
chooser in front of the wizard — the console's **left pane** in its entirety, and the base the
dashboard builds on. `DashboardScreen` is that plus the two right-hand regions, which is what
a bare terminal running `remote-agents tui` still shows.

`DashboardScreen` subclasses the projects picker rather than replacing it, so the filter,
its debounce, the catalogue refresh, and the never-empty stack guarantee are inherited —
what this module adds is the shape: a right-hand column showing the running sessions
(reloaded on reveal and on the same 10-second cadence the sessions list uses, which is also
where the console notices what the other writer did, since `load_sessions` is the sync choke
point) and the notifications feed pane, which reads the durable activity table.

The two lists share the base class's one row-selection handler; session rows carry a
namespaced key (`session:<id>`) so `choose` can route without a second dispatch chain.

DEC-009 note: the sessions pane's and feed pane's empty states are declared and tested
here, in this module's own tests — not by `test_empty_states.py`'s exhaustiveness sweep,
which walks `ChoiceScreen.empty_state` across the registry and therefore covers only the
dashboard's *projects* list. The sweep is exhaustive over positions, not over this
screen's auxiliary panes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.model import _BACK, LaunchSelection
from remote_agents.adapters.tui.rows import (
    limit_rows_content,
    session_contents,
    session_counts_content,
)
from remote_agents.adapters.tui.screens.base import (
    NEVER_EMPTY,
    ChoiceScreen,
    held_option_id,
    restore_highlight_by_id,
)
from remote_agents.adapters.tui.screens.confirm import HostRemoteControlConfirmModal
from remote_agents.adapters.tui.screens.feed import (
    _EMPTY_FEED_ROW,
    FEED_TITLE,
    NO_NOTIFICATIONS,
    FeedRegion,
)
from remote_agents.adapters.tui.screens.launch import ProfilesScreen, ProjectsScreen
from remote_agents.adapters.tui.screens.resume import advance_to_resume_profiles
from remote_agents.adapters.tui.screens.sessions import sessions_title
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_TITLE,
    host_remote_control_directions,
    pair_available,
)
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_views import LimitRow, limit_rows, session_row_parts
from remote_agents.domain.models import SessionRecord
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)

_LOG = logging.getLogger(__name__)

_SESSION_KEY_PREFIX = "session:"
_SESSIONS_AUTO_REFRESH = 10.0

#: The sessions pane's one line when nothing runs — DEC-009's answer for this pane.
_NO_SESSIONS = "No sessions are running."

NO_LIMITS = "No agent limits reported."
"""DEC-009's answer for the limits pane, in the vocabulary the readers already use.

Reached three ways that are one sentence to the owner: a host that wired no reader, a read
that raised, and every installed agent publishing nothing (`opencode` and `cursor-agent`
always, `codex` on a host that has not run it, `claude` when its borrowed cache went stale).
Distinguishing them here would report on this project's plumbing rather than on the owner's
plan.
"""

_EMPTY_LIMITS_ROW = "limits:none"
"""A stable id for the empty row, so a redraw can tell it from an agent's row."""

_LIMITS_ROW_PREFIX = "limits:"

LIMITS_TITLE = "Plan limits"

#: A stable id for the host toggle's line, so a redraw can tell it from an agent's row and a
#: test can find it without counting from the bottom of a list whose length is a provider's.
_HOST_REMOTE_CONTROL_ROW = "limits:host-remote-control"

HOST_REMOTE_CONTROL_KEY = "h"

#: Pairing, on the same screen as the reading it depends on. `P` rather than `p` because
#: the sessions pane advertises `p` for Projects, and a key the frame names for one subject
#: must not quietly mean another -- the same rule that kept the toggle off `m`.
HOST_PAIR_KEY = "P"
"""`h` for *host*, which is the one thing that distinguishes this toggle from `m`.

`m` on a session row toggles Remote Control for that pane; this toggles it for the machine.
Sharing a letter across the two subjects is precisely the confusion `HOST_REMOTE_CONTROL_TITLE`
names the provider to avoid, so they are deliberately different keys.

Free on both screens that carry the line, which is not the same as being free everywhere:
`DashboardScreen` binds `d` and inherits `slash`, `o` and `ctrl+t` from the projects picker;
`LimitsPaneScreen` binds nothing of its own. Neither inherits `SESSION_ACTION_BINDINGS`, whose
letters (`a i r s c f m`) the dashboard's sessions pane nonetheless *advertises* in its border
title -- so those letters are avoided here even though nothing would collide today, because a
key the frame names for one subject must not quietly mean another.

**Not a root binding, so `CONSOLE_BINDINGS` is untouched.** This is a screen binding inside
our own process, exactly as `p` on the sessions pane is, and DEC-041's one-root-key budget
still stands at one -- see `action_show_projects_pane`, which records the same distinction.
"""

#: How each reading of the daemon is put into words, and the whole of why this table exists.
#:
#: `ERRORED` and `UNREACHABLE` derive the same `RemoteControlState` (UNKNOWN) and open the same
#: two directions, so a surface rendering the *state* would spell them identically -- and one
#: did. They are different facts: ERRORED is the daemon answering that its own connection is
#: broken, UNREACHABLE is this project never having had the conversation, which on a host with
#: no `codex` installed is every path at once. An owner told "errored" for the second one can
#: press the toggle forever and never learn the binary is missing. So this keys off the
#: connection, in the surface's own words.
#:
#: `DAEMON_ABSENT` is deliberately not "off" either, for the reason its own docstring gives:
#: the enrollment preference outlives the process that serves it, and "off" is the direction of
#: wrongness an owner acts on by not acting.
#:
#: The direction *labels* are not spelled here -- they are `HOST_REMOTE_CONTROL_LABELS`, and
#: this table is about readings rather than about actions.
#:
#: **They are short because the pane truncates, and truncation is not a cosmetic failure
#: here.** `#limits-pane` is `text-wrap: nowrap; text-overflow: ellipsis`, and its content is
#: 28 columns at an 80-column terminal -- 23 of them spent before the reading begins. The
#: first version of this table read "on, but its connection is broken" for ERRORED and "on,
#: still connecting" for CONNECTING; both painted as `Codex Remote Control · on, …`, so the
#: two were indistinguishable *and* the broken one read as "on". Measured at 80x24, not
#: reasoned about. Every word here is now distinct within its first four characters, and none
#: of them truncates into a different state's word. The nuance those long phrases carried
#: moved to `_HOST_CONNECTION_EXPLANATIONS`, which is rendered where the owner acts rather
#: than in a row that cannot hold it.
_HOST_CONNECTION_WORDS: dict[HostConnection, str] = {
    HostConnection.CONNECTED: "on",
    HostConnection.DISABLED: "off",
    HostConnection.CONNECTING: "connecting",
    HostConnection.DAEMON_ABSENT: "no daemon",
    HostConnection.ERRORED: "link broken",
    HostConnection.UNREACHABLE: "unreachable",
}

#: What to say when the policy opens *both* directions, keyed by the reading that opened them.
#:
#: Both readings mean "we do not know", and they mean it for opposite reasons -- so the remedy
#: is the sentence that actually differs, and it is the half an owner can act on. Consulted
#: with `.get`, because a connection added later that opens two directions must still be
#: refused with the reading rather than with a `KeyError` out of a message handler.
_HOST_AMBIGUOUS_REMEDY: dict[HostConnection, str] = {
    HostConnection.ERRORED: (
        "The daemon is enrolled but says its own connection is broken. Restart it where "
        "codex runs, then press again."
    ),
    HostConnection.UNREACHABLE: (
        "This project could not talk to codex at all — check that it is installed and on "
        "PATH, then press again."
    ),
}

#: What a host that wired no toggle at all reads as. A declared absence is a reading
#: (DEC-009/DEC-061), so the line is drawn and says this rather than being left out -- a
#: missing line is indistinguishable from a surface that forgot to draw one.
_HOST_UNAVAILABLE = "unavailable"


#: The sentence behind each reading, for the screens that have room for one.
#:
#: The pane row cannot hold this and must not try; the bot has carried an equivalent table
#: since Task 3.2 and the terminal had nothing, which left a truncated row as the whole of
#: what a terminal owner could learn. Rendered by the direction chooser, which is the screen
#: an owner reaches precisely when the reading is one they cannot act on confidently.
_HOST_CONNECTION_EXPLANATIONS: dict[HostConnection, str] = {
    HostConnection.CONNECTED: ("This machine is enrolled and a paired phone can reach it."),
    HostConnection.CONNECTING: (
        "The setting has taken and the link to the relay is still settling."
    ),
    HostConnection.DISABLED: ("This machine is not enrolled, so no phone can reach it."),
    HostConnection.DAEMON_ABSENT: (
        "The codex daemon is not running, so nothing here can say whether this machine is "
        "enrolled. The setting outlives the daemon, so this is not the same as off."
    ),
    HostConnection.ERRORED: (
        "The daemon answered and reported that this machine is enrolled but its link to the "
        "relay is broken."
    ),
    HostConnection.UNREACHABLE: (
        "codex did not answer at all, so nothing was read. It may not be installed here."
    ),
}


def host_remote_control_line(status: HostRemoteControlStatus | None) -> str:
    """The limits pane's one line about this machine's host Remote Control.

    Module-level and named so the render can be asserted across all six connections without
    driving a Textual app to reach each one -- the same reason `remote_control_entries` is a
    function beside the screen that calls it rather than a method on it.
    """
    word = _HOST_UNAVAILABLE if status is None else _HOST_CONNECTION_WORDS[status.connection]
    return f"{HOST_REMOTE_CONTROL_TITLE} · {word}"


class HostPairAction(Message):
    """Posted by the pairing key so the code is minted on this screen's own pump.

    Same reason as `HostRemoteControlAction`: the modal this leads to suspends its caller,
    and a binding body runs on the App's pump (DEC-068).
    """


class HostRemoteControlAction(Message):
    """The `h` key, handed to the screen's own pump before anything can suspend on it.

    Posted rather than performed for the reason `RowStopAction` records in full: Textual
    dispatches a screen's non-priority binding from `App._on_key` -> `_check_bindings` ->
    `run_action`, so the action body runs on the **App's** message-pump task. `ask_to_confirm`
    suspends its caller until the modal answers, and suspending there stops the app draining
    messages at all -- observed on the owner's real workstation as a modal that drew correctly
    and then answered no key, including quit.

    DEC-068 is the decision that put a screen binding on DEC-025's list of forbidden callers,
    and this is its required shape: the handler `on_host_remote_control_action` runs on the
    screen's own pump, which is where every confirmation in this tree is raised from.
    """


DASHBOARD_HINT = "enter open · d detail · / filter · o order"
"""The dashboard's muted hint row: the keys that mean something on its resting position."""


class LimitsRegion:
    """The account-wide limits render, shared by the dashboard and the console's own pane.

    A mixin rather than a base class, and for the reason `FeedRegion` next door gives: its two
    users differ in what else they are — the dashboard is the projects position with regions
    bolted on, and the limits pane is nothing else at all. What is shared is the *render*; what
    each surface keeps is placement and chrome.

    Written when the console gained a limits pane of its own. Before that the render lived as a
    private method on `DashboardScreen`, which is exactly why the console had none: the pane is
    composed by a screen only `remote-agents tui` mounts, and the console's three panes are
    three separate processes that never mount it. Copying the method into a fourth would have
    been the second renderer DEC-043 exists to prevent.
    """

    async def _reload_limits(self) -> None:
        """Redraw the account-wide limits, or leave whatever is drawn exactly as it is.

        Failure leaves the pane alone, which is the same contract `_reload_sessions_pane` and
        `_reload_feed` state: the rows already drawn are stale, not wrong, and a background read
        having a bad moment must never blank a pane the owner is reading.

        The rows come from `limit_rows`, so this pane and the bot's block are one decision
        rendered twice rather than two renderers that agree today (DEC-043). What stays here is
        placement, colour and the disabled-row rule -- the surface's half.

        **Two reads, and the host one is not conditional on the other.** The limits reader and
        the host toggle are separate capabilities: a host that wired one and not the other is
        an ordinary composition, so an early return on an absent `limits` would have taken the
        host line off a pane that could still draw it. The redraw happens once, after both, so
        a pane never flickers between one fact and two.
        """
        await self._reload_host_remote_control()
        reader = self.services.backend.limits
        if reader is not None:
            try:
                entries = await reader()
            except Exception:
                _LOG.exception("the agent limits pane could not be reloaded")
            else:
                self._limit_rows = limit_rows(entries)
        self._draw_limits()

    async def _reload_host_remote_control(self) -> None:
        """Re-read this machine's host Remote Control, or leave the last reading drawn.

        `HostRemoteControlService.status()` answers a reading rather than raising -- a boundary
        that would not answer *is* UNREACHABLE -- so the `except` here is for the shapes it
        cannot promise about (a composition wiring something else, a cancelled read), and it
        follows the pane's contract rather than inventing one: what is drawn is stale, not
        wrong, and a background read having a bad moment must never blank a line the owner is
        reading.
        """
        control = self.services.backend.host_remote_control
        if control is None:
            # A declared absence, not a failure (DEC-061). Left as `None` so the line says
            # "unavailable" rather than keeping a reading from a capability that is gone.
            self._host_status = None
            return
        try:
            self._host_status = await control.status()
        except Exception:
            _LOG.exception("this host's Remote Control could not be read")

    def show_host_remote_control(self, status: HostRemoteControlStatus | None) -> None:
        """Draw a reading this region did not read itself.

        The one caller is `RemoteAgentsTui.set_host_remote_control`, which has just been
        handed the daemon's own reading of the change it made. Named rather than reaching
        into `_host_status` from the app, so the pane's one write path is a method the pane
        declares instead of an attribute a caller happens to know about.
        """
        self._host_status = status
        self._draw_limits()

    #: The last successful read, so a resize can re-measure without a provider sweep.
    _limit_rows: tuple[LimitRow, ...] = ()

    #: The last reading of the host toggle. `None` is both "not read yet" and "no capability
    #: wired", which the line renders identically and on purpose: before the first read there
    #: is nothing this surface can honestly claim about the machine either.
    _host_status: HostRemoteControlStatus | None = None

    def _draw_limits(self) -> None:
        """Draw the rows last read, as the grid `rows.limit_row_content` lays out.

        The host toggle's line is drawn **last and always**, under whatever the plan limits
        say, including under the empty sentence: it is a fact about this machine rather than
        about the account, so an account with nothing to report says nothing about whether the
        phone can reach this host.
        """
        found = self.query("#limits-pane")
        if not found:
            return
        pane = found.first(OptionList)
        pane.clear_options()
        host_line = Content(host_remote_control_line(self._host_status))
        if not self._limit_rows:
            # A *successful* read that found nothing is not the same event as a read that
            # raised, and this used to treat them alike -- it returned early, leaving the last
            # figures drawn. That is the routine state, not an exotic one: Claude's borrowed
            # cache is fenced at thirty minutes, so half an hour of idleness pinned a stale
            # percentage on screen permanently, countdown and all, with `(resets in 3h)` still
            # reading 3h four hours later. Worse, the bot correctly dropped its block at the
            # same instant, so the two surfaces asserted different things about one account --
            # the exact divergence sharing `limit_rows` exists to prevent.
            pane.add_option(Option(NO_LIMITS, id=_EMPTY_LIMITS_ROW, disabled=True))
            self._add_host_row(pane, host_line)
            _fit_to_content(pane, (Content(NO_LIMITS), host_line))
            return
        width = pane.content_size.width
        if width <= 0:
            # Drawn inside `populate`, before the pane has been laid out: the lines are built
            # for an unknown width now and rebuilt for the real one after the first refresh --
            # the same deferral `_fit_to_content` makes for its measurement.
            pane.call_after_refresh(self._draw_limits)
        contents = limit_rows_content(self._limit_rows, width or None)
        for index, content in enumerate(contents):
            pane.add_option(Option(content, id=f"{_LIMITS_ROW_PREFIX}{index}", disabled=True))
        self._add_host_row(pane, host_line)
        _fit_to_content(pane, (*contents, host_line))

    def _add_host_row(self, pane: OptionList, line: Content) -> None:
        """The host line, disabled like every other row here.

        Disabled although it is the one row in this pane that *does* something, and that is
        deliberate: what acts is the key, not the row. Every row in this pane reports a fact
        and none of them opens on Enter, so a cursor able to rest on exactly one of them would
        answer Enter with silence on the other four -- which reads as a broken key rather than
        as a pane that never had anything to open.
        """
        pane.add_option(Option(line, id=_HOST_REMOTE_CONTROL_ROW, disabled=True))


class ProjectsPaneScreen(ProjectsScreen):
    """The projects position with the chooser in front of the wizard — the console's left pane.

    The projects picker on its own sends a chosen project straight into the agent list. This
    screen is the one that asks the Launch-or-Resume question first (DEC-033: navigation over
    flows both surfaces already have, never a new wizard step), and it exists as its own class
    because two surfaces need exactly that behavior: the console's **left pane**, which is its
    whole content, and the combined dashboard, which adds the two right-hand regions on top.

    `DashboardScreen` therefore subclasses *this* rather than `ProjectsScreen`. The alternative
    — each surface routing a chosen project itself — is one behavior written twice, and the two
    copies would only have to disagree once.
    """

    def __init__(self) -> None:
        super().__init__()
        #: What the console's start-only repair could not put right, held so it can be
        #: restated after every redraw — see `_restate_blocked`.
        self._blocked: tuple[str, ...] = ()

    async def populate(self) -> None:
        await super().populate()
        self._report_console_recovery()

    def render_projects(self, query: str = "", *, keep_focus: bool = False) -> None:
        super().render_projects(query, keep_focus=keep_focus)
        self._restate_blocked()

    def _report_console_recovery(self) -> None:
        """Tell the owner what the console's start-time repair did, and what it could not.

        The two halves get different destinations because they are different facts, and this
        surface already has a rule for each. `moved` is something that already happened and is
        finished — a confirmation, which `announce` puts in a toast. `blocked` is what needs a
        *person*; anything the owner must keep belongs in the status line, which is the rule
        the attach command is already handled by.

        Before this, neither reached them at all: `moved` was logged at INFO with no logging
        configured anywhere in `src/`, and `blocked` was printed to stderr in the instant
        before Textual took the alternate screen.
        """
        report = self.services.console_recovery
        if report is None:
            return
        for note in report.moved:
            self.tui.announce(f"The console was restored: {note}", severity="information")
        self._blocked = tuple(report.blocked)
        self._restate_blocked()

    def _restate_blocked(self) -> None:
        """Put the blocked notes back after a redraw, because the condition still holds.

        `render_projects` writes the ordinary status on every fill, so a note set once would
        be gone by the first refresh — and unlike a failed read, this is not a stale fact that
        a redraw supersedes. Nothing in this process is going to fix it.
        """
        if not self._blocked:
            return
        # An f-string rather than a concatenation, and the difference is not style: the
        # status-region sweep reads this call's first argument out of the AST to check that a
        # severity-coloured status carries words of its own, and a `BinOp` is a shape it
        # cannot read — so it reports it as colour with no words, correctly.
        notes = " · ".join(self._blocked)
        self.set_status(
            f"The console could not be fully restored: {notes}",
            severity="warning",
        )

    #: This pane is its own process's resting position, so escape here is inert and there is
    #: no other position in the process to return to.
    read_failure_route = "Ctrl+R re-reads this screen."

    async def choose(self, key: str) -> None:
        """A project row opens the chooser.

        The selection is deliberately not touched here: only Launch commits to a fresh one, so
        backing out of the chooser must leave nothing behind. Opening is not a stop, so the
        resting-cursor discipline (DEC-007) is untouched — no row here mutates anything.
        """
        project = next((item for item in self.tui.catalogue if item.opaque_id == key), None)
        if project is None:
            self.announce(
                "That project is no longer available. Refresh and try again.", severity="warning"
            )
            return
        await self.advance_to(ProjectChooserScreen(project))


class LimitsPaneScreen(LimitsRegion, ChoiceScreen):
    """The console's right-middle pane: the account's rate-limit windows and nothing else.

    **This is the surface the owner's second ask actually named.** "Put them in the TUI too, on
    the right, between the sessions and notifications panes, in their own pane" describes the
    three-pane console — its right column *is* sessions over notifications — and the console is
    what `remote-agents` runs with no arguments. The pane was first built on `DashboardScreen`,
    which only `remote-agents tui` mounts, so the one arrangement the words map onto was the one
    arrangement without it.

    A `ChoiceScreen` for the reason `FeedScreen` is one: that is what carries this surface's
    chrome — the status region, the never-empty stack guarantee, `check_action`, the breadcrumb
    — and its choice list is composed and hidden, because `ChoiceScreen`'s machinery queries
    `#filter`, `#choices` and `#output` by id and must find what it expects.

    It offers no flow and takes no keys of its own. Every limit here is a *read*: nothing on
    this pane mutates anything, which is why it is the one console pane that needs no
    confirmation, no busy interlock and no policy.

    **That still holds now the pane carries the host toggle's line, and it is why the key is
    not here.** The line is a reading like every other row, drawn by the shared region; the
    key that changes it is bound on `DashboardScreen` alone. Binding it here would give this
    pane its first mutating action and with it the confirmation, the busy interlock and the
    policy the paragraph above says it does not have — and the confirmation could not be
    shared with the dashboard's in any case, because the architecture sweep behind DEC-025
    requires the method that asks it to be defined on a screen rather than on this region's
    mixin. Under console hosting the toggle is therefore the phone's or the dashboard's, and
    an owner reading the console's pane learns the state without being offered the change.
    """

    #: This pane can be empty and says so in its own words (DEC-009) — a host whose providers
    #: publish nothing, or a Claude cache past its staleness fence, is the routine state rather
    #: than an exotic one.
    empty_state = NO_LIMITS

    position = "LIMITS_PANE"
    can_refresh = True
    crumb = "Agent limits"
    status = "What each agent has spent against its plan, for the whole account."
    read_failure_route = "Ctrl+R re-reads this screen."

    #: The dashboard refreshes limits on a sixty-second timer; this pane keeps the same cadence
    #: rather than a livelier one. The windows move on the hour, and every reader is a file the
    #: provider was going to write anyway (DEC-061) — a faster tick would buy nothing and cost a
    #: directory sweep per interval in a process that exists to be glanced at.
    _LIMITS_AUTO_REFRESH = 60.0

    DEFAULT_CSS = """
    LimitsPaneScreen #filter { display: none; }
    LimitsPaneScreen #choices { display: none; }
    /* The status region is two rows fixed (`ChoiceScreen #status`, sized for a sentence that
       wraps on a narrow terminal) and this pane's status never changes and is never written
       to: `_reload_limits` reports a failed read to the log, and the empty read to the list
       itself, so nothing here has ever put a word in it. Two rows to restate a heading, on a
       pane whose content is two lines. */
    LimitsPaneScreen #status { display: none; }
    /* And no border, where every other list in this app has one. `OptionList` draws its own,
       which is what carried "Agent limits" once the header went -- but a title costs two rows
       here and the rows underneath it already begin `claude:` and `codex:`, which is the same
       fact in the space it was already taking. The empty state says "No agent limits
       reported.", so the pane names itself when it holds nothing too.

       `height: 1fr` is only what the pane shows before its first measurement: `_fit_to_content`
       overwrites it with the rows the content actually wraps to, and with no border to allow
       for that is now the pane's whole height. */
    LimitsPaneScreen #limits-pane {
        height: 1fr; border: none; text-wrap: nowrap; text-overflow: ellipsis;
    }
    LimitsPaneScreen #hint { display: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._timer = None

    def compose(self) -> ComposeResult:
        """The base body with the limits list in place of the list; same ids, no app chrome.

        **No `Header` and no `Footer`, which every other position composes.** Both are
        `ChoiceScreen`'s, inherited with the machinery this screen subclasses it for, and on a
        pane that never navigates they draw two fixed lines: the header renders `crumb`, a
        class constant here, over a title the pane beside it also shows -- and the list's own
        border already says "Agent limits". The footer advertised `ctrl+q` and `ctrl+r`; both still
        work, because they are the app's bindings and nothing here unbinds them, and the one
        that matters is the one the status line already names when a read fails.

        Two rows of a pane that holds four short lines, which is what they cost.
        """
        with Vertical(id="body"):
            yield Static(self.status, id="status", markup=False)
            yield Static("", id="hint", classes="-empty", markup=False)
            yield Input(placeholder="", id="filter")
            yield OptionList(id="choices", markup=False)
            # Seeded with its empty state at compose time for the reason `FeedScreen` gives:
            # `_reload_limits` returns early when the capability is absent or the read raises,
            # so an unseeded list would answer both with an empty box instead of the sentence
            # DEC-009 requires this pane to declare.
            pane = OptionList(
                Option(NO_LIMITS, id=_EMPTY_LIMITS_ROW, disabled=True),
                id="limits-pane",
                markup=False,
            )
            pane.border_title = LIMITS_TITLE
            yield pane
            with VerticalScroll(id="output-pane"):
                yield TextArea(
                    "", id="output", read_only=True, soft_wrap=True, highlight_cursor_line=False
                )

    def on_resize(self, event: events.Resize) -> None:
        self._draw_limits()

    async def populate(self) -> None:
        self.hide_entry()
        await self._reload_limits()
        if self._timer is None:
            self._timer = self.set_interval(self._LIMITS_AUTO_REFRESH, self._auto_reload)

    async def on_reveal(self) -> None:
        await self._reload_limits()

    async def refresh_contents(self) -> None:
        """Ctrl+R re-reads the providers' files, which is the whole of what this pane shows."""
        await self._reload_limits()

    async def _auto_reload(self) -> None:
        """The interval's own call, guarded on still being the screen the owner is looking at.

        `_reload_limits` is guarded against a failed read; this is guarded against a *pointless*
        one. Without it a suspended pane keeps sweeping the providers' directories every minute
        in a process nobody is looking at — the cost DEC-065 records paying by hand once the
        timer moved off the screen that owned it.
        """
        if not self.showing:
            return
        await self._reload_limits()

    def on_screen_suspend(self) -> None:
        if self._timer is not None:
            self._timer.pause()

    def on_screen_resume(self) -> None:
        if self._timer is not None:
            self._timer.resume()


class DashboardScreen(LimitsRegion, FeedRegion, ProjectsPaneScreen):
    """Three panes, one resting position; everything the projects picker was, plus sight."""

    draws_session_rows = True
    """This screen renders session rows, so the app's gauge cache is worth refreshing while it
    is the one showing. Read by `RemoteAgentsTui._refresh_context_windows_tick`; screens without
    it cost no provider read at all."""

    position = "DASHBOARD"

    BINDINGS = [
        # Hidden from the footer: the bar is shared with every inherited binding and the
        # key only means something while the sessions pane holds a highlighted row — the
        # sessions pane's border title advertises it instead, where it is true.
        Binding("d", "session_detail", "Session detail", show=False),
        # The host toggle, on the screen that draws the line it changes. Hidden for the same
        # reason `d` is, and labelled from the application's title rather than spelled here:
        # the two Remote Controls share one vocabulary by identity, not by agreement (DEC-007).
        Binding(
            HOST_REMOTE_CONTROL_KEY, "host_remote_control", HOST_REMOTE_CONTROL_TITLE, show=False
        ),
        # Pairing, hidden for the same reason and offered only where the policy says a code
        # could pair anything -- the key exists on the screen, the availability is checked
        # when it is pressed.
        Binding(HOST_PAIR_KEY, "host_pair", f"{HOST_REMOTE_CONTROL_TITLE} pairing", show=False),
    ]

    #: The dashboard is the projects position, so its crumb is that position's, and the
    #: redesign's header names the surface after it: `Projects › dashboard`.
    crumb = "Projects › dashboard"

    # Unchanged proportions -- 3fr / 2fr, and the right column's 2fr / 40% / 1fr. What the
    # redesign changed: all four panes framed alike, the projects list included (it used to be
    # the one unframed list), and an even one-cell gutter between them -- `margin: 0 1` on the
    # left column, `margin-top: 1` between the right-hand panes.
    DEFAULT_CSS = """
    DashboardScreen #dashboard-panes { height: 1fr; }
    DashboardScreen #dashboard-left { width: 3fr; margin: 0 1; }
    DashboardScreen #dashboard-right { width: 2fr; }
    DashboardScreen #choices {
        border: round $secondary; text-wrap: nowrap; text-overflow: ellipsis;
    }
    DashboardScreen #sessions-pane {
        height: 2fr; border: round $secondary; text-wrap: nowrap; text-overflow: ellipsis;
    }
    DashboardScreen #limits-pane {
        max-height: 40%; border: round $secondary; margin-top: 1;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    DashboardScreen #feed-pane {
        height: 1fr; border: round $secondary; margin-top: 1;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._sessions_timer: Timer | None = None
        self._reloading_sessions = False
        self._resumed_before = False
        #: The rows last drawn, so a resize re-measures the columns without a store read.
        self._session_records: tuple[SessionRecord, ...] = ()

    def _describe_projects(self, count: int) -> None:
        """Nothing: the dashboard's status is the sessions' counts (`_draw_session_rows`), and
        its hint names every pane's keys at once. The project count and order moved to the
        projects pane's own title, where `render_projects` writes them for every position."""

    def on_resize(self, event: events.Resize) -> None:
        """Every pane lays its columns out to a measured width; re-measure all four."""
        super().on_resize(event)
        if not self.showing:
            return
        self._draw_session_rows(self._session_records)
        self._draw_limits()
        self.redraw_feed()

    def compose(self) -> ComposeResult:
        """The base body, re-arranged: same ids, so every inherited method still lands.

        `#status`, `#filter`, `#choices`, and `#output` keep their names and their bases'
        widget classes — the machinery in `ChoiceScreen` queries them by id and must find
        exactly what it expects. Only their arrangement is new.
        """
        yield Header()
        with Vertical(id="body"):
            yield Static(self.status, id="status", markup=False)
            yield Static("", id="hint", classes="-empty", markup=False)
            with Horizontal(id="dashboard-panes"):
                with Vertical(id="dashboard-left"):
                    yield Input(placeholder=self.filter_placeholder or "", id="filter")
                    yield OptionList(id="choices", markup=False)
                with Vertical(id="dashboard-right"):
                    sessions = OptionList(id="sessions-pane", markup=False)
                    # The count is written in by every draw; the letters are the row keys,
                    # advertised on the frame of the list they act on.
                    sessions.border_title = sessions_title(0)
                    yield sessions
                    # Between the sessions and the notifications, which is where the owner
                    # asked for it on 2026-08-29. Seeded with its empty state at compose time
                    # for the reason `#feed-pane` below is: `_reload_limits` returns early on
                    # an absent capability and on a raising read, so an unseeded pane would
                    # answer both with an empty box instead of the sentence DEC-009 requires.
                    #
                    # Every row is disabled. The pane reports a fact and offers no action, so a
                    # cursor resting here would answer Enter with silence -- which reads as a
                    # broken key rather than as a pane that never had anything to open.
                    limits = OptionList(
                        Option(NO_LIMITS, id=_EMPTY_LIMITS_ROW, disabled=True),
                        id="limits-pane",
                        markup=False,
                    )
                    limits.border_title = LIMITS_TITLE
                    yield limits
                    # Seeded with its empty state at compose time, not left blank. `_reload_feed`
                    # returns early when the capability is absent or the read raises, and a
                    # `Static` used to carry this sentence as its initial content -- so an
                    # unseeded list would answer both of those with an empty box instead of the
                    # sentence DEC-009 requires this pane to declare.
                    feed = OptionList(
                        Option(NO_NOTIFICATIONS, id=_EMPTY_FEED_ROW, disabled=True),
                        id="feed-pane",
                        markup=False,
                    )
                    feed.border_title = FEED_TITLE
                    yield feed
            with VerticalScroll(id="output-pane"):
                yield TextArea(
                    "", id="output", read_only=True, soft_wrap=True, highlight_cursor_line=False
                )
        yield Footer()

    async def choose(self, key: str) -> None:
        """A session row opens the session itself; anything else is a project row.

        A session row routes through the app's one open seam, so hosting decides what
        opening means — an exchange, or the exec handoff — and this screen never has to
        know. The project half is `ProjectsPaneScreen.choose`, unchanged and shared with the
        console's left pane rather than copied into it.
        """
        if key.startswith(_SESSION_KEY_PREFIX):
            await self.tui._open_or_leave(key.removeprefix(_SESSION_KEY_PREFIX))
            return
        await super().choose(key)

    async def action_session_detail(self) -> None:
        """`d` on the highlighted session row opens today's detail screen unchanged.

        Every stop, inspect, rename, and Remote Control affordance lives there, so the
        dashboard narrows nothing DEC-007's full control plane promised — opening is the
        fast path, the detail is one key away.
        """
        pane = self.query_one("#sessions-pane", OptionList)
        index = pane.highlighted
        if index is None or pane.option_count <= index:
            return
        key = pane.get_option_at_index(index).id
        if key is None or not key.startswith(_SESSION_KEY_PREFIX):
            return
        await self.tui.show_detail(key.removeprefix(_SESSION_KEY_PREFIX))

    def action_host_remote_control(self) -> None:
        """Hand the key to this screen's own pump and return immediately.

        **Nothing is awaited here, and that is the whole of DEC-068.** A screen's binding is
        dispatched from `App._on_key`, so this body runs on the *App's* message-pump task --
        and the confirmation this key leads to suspends its caller until the owner answers.
        Suspending here suspends the application: the modal draws and then no key works,
        quit included. `on_host_remote_control_action` is where the work happens, on this
        screen's own pump, which is the shape DEC-025 says makes every other confirmation in
        this tree safe.
        """
        self.post_message(HostRemoteControlAction())

    async def on_host_remote_control_action(self, message: HostRemoteControlAction) -> None:
        """The screen handler `HostRemoteControlAction` is delivered to."""
        del message
        await self.confirm_host_remote_control()

    def action_host_pair(self) -> None:
        """Hand the pairing key to this screen's own pump, for DEC-068's reason exactly."""
        self.post_message(HostPairAction())

    async def on_host_pair_action(self, message: HostPairAction) -> None:
        """The screen handler `HostPairAction` is delivered to."""
        del message
        await self.confirm_host_pair()

    async def confirm_host_pair(self) -> None:
        """Mint one pairing code and show it once.

        **No confirmation stands in front of this**, and that is a decision rather than an
        omission. A confirmation exists to stop an owner changing something by accident;
        minting a code changes nothing about the machine -- it is a read that happens to
        produce a secret. What the owner needs is not to be asked twice but to be *told*,
        which the modal does: what it is, when it dies, and that anyone holding it can drive
        this machine.

        **Availability is re-read, not taken from the drawn line.** The line is up to one
        reload old, and `pair_available` is false wherever there is no live relay link -- a
        code minted then would expire unused and read to an owner as a broken feature rather
        than as an action that was never offered.

        The code is never returned from here, never announced, and never stored. It exists on
        the modal for as long as the modal is on screen and nowhere else (DEC-013).
        """
        if self.tui.busy or self.services.backend.host_remote_control is None:
            return
        async with self.holding_the_guard():
            await self._reload_host_remote_control()
            if not self.showing:
                return
            self._draw_limits()
            if not self.host_pair_offered(self._host_status):
                self.announce(
                    f"{host_remote_control_line(self._host_status)}. "
                    "Pairing needs a live connection to pair to.",
                    severity="warning",
                )
                return
        await self.tui.pair_host_remote_control(self)

    def host_directions_offered(
        self, status: HostRemoteControlStatus | None
    ) -> tuple[RemoteControlState, ...]:
        """Which directions this screen will act on for `status`.

        The policy's answer, returned rather than re-derived, and consulted by the real key
        path below so the two cannot disagree. It exists as a method because the terminal's
        directions live in what the key is willing to do rather than in a keyboard a parity
        test could scrape -- the bot has buttons to read; this is its equal.
        """
        return host_remote_control_directions(status)

    def host_pair_offered(self, status: HostRemoteControlStatus | None) -> bool:
        """Whether this screen will mint a code for `status`. Same reason as above."""
        return pair_available(status)

    async def confirm_host_remote_control(self) -> None:
        """Re-read the machine, offer the one open direction, and issue only on a `True`.

        Shaped after `SessionDetailScreen.confirm_remote_control` and guarded for the same
        reasons: the guard is held across the re-read *and* the whole modal, so an Escape
        landing mid-read cannot pop this screen out from under a question already on its way,
        and it is released before the call that takes it itself.

        **DEC-052 is satisfied by construction rather than by a mitigation.** That decision is
        about a mutating key acting on whichever row the cursor happens to be on; this key
        reads no row at all. Its subject is the machine, it is fixed before the question is
        asked, and the modal names the direction it is asking about.

        **The direction comes from a fresh read, not from the drawn line.** The line is up to
        one reload old, and a direction taken from it would be a decision made against a
        reading the owner may have been looking at for a minute. Where the policy opens *two*
        directions -- ERRORED and UNREACHABLE, the two readings that mean "we do not know" --
        this refuses in words rather than picking one: choosing a side of a question the
        policy deliberately declines to answer would be guessing about a machine-wide setting.
        The refusal names the reading and the remedy, which is the thing an owner pressing a
        button most needs and the one this line exists to stop withholding.

        The cost of that refusal, stated because it is real: from those two readings the
        terminal offers no way to re-assert a direction, so an owner whose daemon is wedged
        fixes it where codex runs rather than from here.
        """
        if self.tui.busy or self.services.backend.host_remote_control is None:
            # A dead-end entry is worse than an absent one: a host that wired no toggle draws
            # the line saying so and the key does nothing, rather than opening a question
            # nothing could answer.
            return
        async with self.holding_the_guard():
            await self._reload_host_remote_control()
            if not self.showing:
                return
            self._draw_limits()
            status = self._host_status
            directions = self.host_directions_offered(status)
            if not directions:
                return
            chosen = directions[0]
            if len(directions) > 1:
                # The policy declined to say which way the host is set, so this asks rather
                # than guessing -- and rather than refusing, which is what it used to do. The
                # bot renders both directions as buttons, and a surface offering a smaller
                # set than its sibling is exactly the drift DEC-007's parity contract exists
                # to catch. It caught this one.
                remedy = _HOST_AMBIGUOUS_REMEDY.get(status.connection, "") if status else ""
                if remedy:
                    self.announce(
                        f"{host_remote_control_line(status)}. {remedy}".strip(),
                        severity="warning",
                    )
                explanation = (
                    _HOST_CONNECTION_EXPLANATIONS.get(status.connection, "") if status else ""
                )
                picked = await self.tui.ask_for_host_direction(directions, explanation)
                if picked is None:
                    return
                chosen = picked
            try:
                confirmed = await self.tui.ask_to_confirm(
                    HostRemoteControlConfirmModal.for_direction(chosen)
                )
            except Exception as error:
                _LOG.exception("the host Remote Control confirmation could not be shown")
                self.announce(f"The confirmation could not be shown: {error} Nothing was changed.")
                return
            if not confirmed:
                return
        await self.tui.set_host_remote_control(chosen, self)

    async def populate(self) -> None:
        await super().populate()
        if self.services.open_in_console is not None:
            # The console's one root key, documented where the owner rests. It said "F12
            # returns to this dashboard", which was true of the tab model and is not true
            # now: F12 brings the console's **projects pane** back to the left slot, and
            # this combined dashboard is a different surface that a console never runs.
            # Under console hosting it is still worth naming, because the key is bound on
            # this server and will act whatever surface is looking at it.
            self.sub_title = "F12 shows the console's projects pane"
        records = await self._reload_sessions_pane()
        # The interval is installed *before* the gauge read, not after. Awaiting a provider read
        # first meant a stalled filesystem -- an NFS dev root, a sleeping disk -- left this line
        # unreached, so the ten-second repaint never started at all and the pane sat frozen at
        # its first snapshot for the life of the process, with no error anywhere.
        self._sessions_timer = self.set_interval(_SESSIONS_AUTO_REFRESH, self._auto_reload_sessions)
        # The gauges land on the screen the owner opens on rather than a minute later -- from
        # the records that draw already read, not from a second read of the store.
        if records:
            await self.tui.refresh_context_windows(records)
            self._draw_session_rows(records)

    async def on_reveal(self) -> None:
        await super().on_reveal()
        await self._reload_sessions_pane()

    def on_screen_suspend(self) -> None:
        if self._sessions_timer is not None:
            self._sessions_timer.pause()

    def on_screen_resume(self) -> None:
        if self._sessions_timer is not None:
            self._sessions_timer.resume()
        # The first resume is the screen's own activation at mount, where populate() has
        # just made (or is about to make) the first fill; scheduling another read there
        # doubled every startup — measured by the flaky-store test's read budget.
        if not self._resumed_before:
            self._resumed_before = True
            return
        # Resuming is also the moment the pane is most likely stale: the flow that just
        # popped may have launched or stopped a session, and the return path lands here
        # without passing the reveal hook. Waiting out the timer left the owner reading
        # "No sessions are running." for up to ten seconds beside a session that
        # already existed — measured live by the Stage 4 gate evaluator.
        self.call_later(self._reload_sessions_pane)

    async def _auto_reload_sessions(self) -> None:
        if not self.showing or self.tui.busy or self._reloading_sessions:
            return
        await self._reload_sessions_pane()

    async def _reload_sessions_pane(self) -> tuple[SessionRecord, ...] | None:
        """Redraw the sessions pane from a fresh read, keeping the cursor on its row.

        Failure leaves the pane as it was: the rows already drawn are stale, not wrong, and
        the resting position must never break because a background read had a bad moment.

        Answers the records it drew, so a caller that needs them does not read the store a
        second time. `populate` does exactly that for the context gauges: reading twice at
        mount was both wasteful and visible -- it consumed one call of the flaky-store
        double's budget and moved a failure the suite pins onto a different read.
        """
        if self._reloading_sessions:
            return None
        self._reloading_sessions = True
        try:
            records = await self.tui.load_sessions()
        except Exception:
            _LOG.exception("the dashboard sessions pane could not be reloaded")
            # The right column is refreshed anyway. This early return used to skip
            # `_reload_limits`, which is the dashboard's only refresh path for the host
            # Remote Control reading -- so while the session store was failing, the host line
            # froze at its last value with nothing on screen saying so, and a toggle made
            # from the phone could leave the terminal reading "on" indefinitely. The two
            # reads answer different questions of different sources; one failing is not a
            # reason to stop asking the other.
            await self._reload_limits()
            return None
        finally:
            self._reloading_sessions = False
        # The feed and the limits pane ride the same cadence: one schedule, three panes, so
        # the right column cannot drift out of step with itself.
        await self._reload_limits()
        # The feed rides the same cadence: one schedule, two panes. Its failure mode is
        # the placeholder, never a broken resting position. Known blind spot, inherited
        # from the pane's own suspend behavior: while a flow screen is pushed above the
        # dashboard the timer is paused, so no flash fires until the flow pops — the same
        # trade the sessions pane's staleness note records.
        await self._reload_feed()
        self._draw_session_rows(records)
        return records

    def _draw_session_rows(self, records: tuple[SessionRecord, ...]) -> None:
        """Draw the rows alone, without re-reading the other two panes.

        Split out so the context-gauge refresh can put new figures on screen without dragging
        the limits and notifications reads along with it. Folding them together doubled both on
        every gauge tick, and doubled them at mount -- caught by the limits pane's own
        one-read-at-mount check.
        """
        self._session_records = records
        pane = self.query_one("#sessions-pane", OptionList)
        held_id = held_option_id(pane)
        pane.clear_options()
        pane.border_title = sessions_title(len(records))
        if not records:
            pane.add_option(Option(_NO_SESSIONS, id="empty", disabled=True))
            self.set_status(_NO_SESSIONS, hint=DASHBOARD_HINT)
            return
        # The status is the counts and the hint is the keymap, both written here because the
        # sessions are what the counts describe and this is the one place they are drawn from
        # (one read, never two).
        self.set_status(session_counts_content(records), hint=DASHBOARD_HINT)
        parts = [
            session_row_parts(record, self.tui.context_window_for(record.session_id))
            for record in records
        ]
        contents = session_contents(parts, pane.content_size.width or None)
        for record, content in zip(records, contents, strict=True):
            pane.add_option(
                Option(
                    # The gauge rides inside `session_row_parts`, drawn only for a running
                    # session with a known ceiling; the id stays the record's, so DEC-007's
                    # cursor restoration is unaffected by what the row now says.
                    content,
                    id=f"{_SESSION_KEY_PREFIX}{record.session_id}",
                )
            )
        # The cursor always rests somewhere (DEC-007's discipline, as show_choices keeps
        # it for every #choices list): on the row it held if that row survived the
        # rebuild, else on the first row — a pane advertising "enter opens" with no
        # highlighted row makes both keys silent no-ops until an arrow press.
        restore_highlight_by_id(
            pane, held_id, [f"{_SESSION_KEY_PREFIX}{record.session_id}" for record in records]
        )


def _fit_to_content(pane: OptionList, lines: Iterable[Content]) -> None:
    """Give the pane exactly the rows its wrapped text needs, and no more.

    `height: auto` does not track an `OptionList`'s content here -- measured at several sizes,
    the pane held the same height with nothing drawn and with eight rows, so `max-height` alone
    decided it and the box sat mostly blank while taking rows from the two panes either side.
    Four readers is the production ceiling, so the blank-heavy render was the normal case, not
    an edge.

    Counting **wrapped** rows rather than lines is the part that has to be right. Sizing to
    `len(lines)` looked correct on a wide terminal and cut the continuation of every wrapped
    line on a narrow one -- which took the borrowed-cache stamp off the screen at 70 columns,
    the one thing DEC-061 requires presentation to say out loud. The width is read off the pane
    the way `_continuation_rows` reads the feed's, and is 0 until the first layout, where
    leaving the height alone lets `max-height` cover a frame nobody has seen yet.
    """
    rows = tuple(lines)

    def measure() -> None:
        width = pane.content_size.width
        if width <= 0:
            return
        # A lost row here would be unreachable rather than untidy: every option is disabled, so
        # the highlight never moves and no key scrolls to it -- which is why the rows are
        # broken to the width *before* they are counted, and never wrapped by the widget.
        # One row per line: `limit_row_content` has already broken each agent's windows into
        # lines that fit the pane, and the pane draws them `nowrap`, so a line longer than the
        # pane (a single window wider than the whole column) is ellipsised rather than wrapped.
        rows_high = len(rows)
        # The widget's own gutter, not a literal 2. It *was* a literal, and it meant "the
        # border this list draws" -- true of both panes sharing this render until the console's
        # dropped its border, where a hardcoded 2 would have left two blank rows inside a pane
        # sized to have none. `gutter` is padding plus border plus scrollbars, which is exactly
        # the height the wrapped rows do not get to use, asked of the widget that knows.
        pane.styles.height = rows_high + pane.gutter.height

    # Deferred, because the first draw happens inside `populate()` -- before the pane has been
    # laid out, so its width is still 0 and a measurement taken there silently does nothing.
    # That is not a rare frame: it is the view the owner opens on, and it left the pane at its
    # `max-height` until the ten-second tick came round and re-measured it.
    pane.call_after_refresh(measure)


class ProjectChooserScreen(ChoiceScreen):
    """Launch new, or reopen saved — one question per project, then the existing flows.

    Navigation over flows both surfaces already have, never a new wizard step (DEC-033):
    Launch restarts the launch wizard with a fresh selection exactly as the projects
    picker always did, and Resume enters the same guarded capability fetch the resume
    flow's own project picker uses. A host with no conversations service never shows
    Resume at all — a dead-end entry is worse than an absent one (DEC-009's spirit).
    """

    #: Launch is always offered, so this position cannot be empty by construction.
    empty_state = NEVER_EMPTY
    position = "PROJECT_CHOOSER"
    status = "Launch a new session, or reopen a saved conversation."

    def __init__(self, project: CatalogProject) -> None:
        super().__init__()
        self.project = project

    @property
    def crumb(self) -> str:
        """The project just chosen — the one fact the trail cannot already carry."""
        return f"{self.project.area}/{self.project.name}"

    async def populate(self) -> None:
        self.hide_entry()
        entries: tuple[tuple[str, str], ...] = (("launch", "Launch a new session"),)
        if self.services.backend.conversations is not None:
            entries = (*entries, ("resume", "Resume a conversation"))
        self.show_choices((*entries, (_BACK, "Back")))

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
            return
        if key == "launch":
            # A fresh selection rather than a patched one, exactly as the projects picker
            # committed it: an agent left from an abandoned pass must not survive.
            self.tui.selection = replace(LaunchSelection(), project=self.project)
            await self.advance_to(ProfilesScreen())
            return
        if key == "resume":
            await advance_to_resume_profiles(self, self.project)
