"""Settings: what this machine does, and what this terminal remembers.

Five rows in two groups. The first three have the **machine** as their subject -- whether each
provider's next session starts remote-controlled, and where Claude's account limits are read
from -- and the last two have this **terminal** as theirs: its theme and the order it lists
projects in. Nothing here is about a session or a launch, which is what makes it a screen and
not a line on one of the panes.

The groups are the draw order and nothing else. No headings and no separators: the screen is
short enough that a heading would be a bigger thing to read than the rows it introduced, and
the subjects are legible from the row titles -- three name a provider, two name the surface.

**Why this is a screen of its own rather than a line on an existing pane.** The first facts it
carries have the *machine* as their subject, not a session and not a launch. Claude resolves
`remoteControlAtStartup` from its own settings file at every start, so the value governs every
`claude` session on this host, including ones the owner opens by hand; Codex's enrollment is a
persisted daemon preference that governs every `codex` one. Neither can be hung off a session
row without claiming to be about that session, and the limits pane -- the one region that does
describe the host -- already carries the Codex *reading* and has no room to carry an action per
provider (its rows truncate at 28 columns on an 80-column terminal, measured). So the two sit
here as two rows of one kind, which is the shape the premise check established they are
(`docs/acceptance-2026-09-11-surface-refresh.md` section 8, plan Stage 3).

**The exception this row used to carry is gone as of 0.41.0.** While the `claude-remote` profile
existed it was `claude --remote-control {managed_name}`, and that flag forces Remote Control on
*over* a settings file saying `false` -- measured, section 8 arm G -- so a launch on that profile
came up connected whatever this row said. That is the one direction of wrongness that matters: the
row reads *off* while the pane is on. Stage 4 closed it from both ends. The profile is retired
(Task 4.4), and a `claude` launch now reads this row and carries the flag only on an explicit *on*
(Task 4.2), so no launch this project makes can contradict it. The row's claim is every `claude`
session on the host, including one started by hand at the keyboard, because the value it shows is
the one Claude itself resolves at startup. The narrower claim was found by the Stage 3 gate's
evaluator, which was right about the code as it stood then.

**Every word on every row is the application's, and so is the Claude row's renderer.**
`REMOTE_CONTROL_DEFAULT_TITLE` and `REMOTE_CONTROL_DEFAULT_LABELS` spell the Claude row and
`remote_control_default_line` assembles it -- all three in
`application/remote_control_default.py`, not here, because the bot's `/settings` screen renders
that same row and `tests/architecture/check_imports.py` confines each driver adapter to its own
subtree, so a line the bot must also draw cannot live in the terminal's. `HOST_REMOTE_CONTROL_TITLE`
and `host_remote_control_line` spell the Codex one, and the bot's `/settings` screen renders the
first three of these rows from the same tables -- the last two are this terminal's alone.
That is DEC-007's point rather than a tidiness preference:
two surfaces that *agreed* about a wording would be free to stop agreeing, and this row's
vocabulary is the one place in the project where a wrong word is acted on by not acting --
`PROVIDER_DEFAULT` worded as any form of "off" would tell the owner their panes come up
disconnected when the measured behaviour is that they come up connected.

**The rows are deliberately not symmetrical in what a press costs, and each argues its own
case where it is declared.** The Claude row writes
a key into a file and the next `claude` start reads it: reversible, three presses back to where
it began, so it asks nothing. The Codex row changes a daemon this machine is enrolled with, so
it confirms -- and its confirmation is raised from `choose`, a screen handler on this screen's
own message pump, never from a binding body (DEC-025 as DEC-068 extends it). An `await` on a
modal from the App's pump stops the surface answering anything at all, quit included, with the
modal drawn correctly because the modal is the last thing it manages to draw. The limits-source
row is the one that most looks like it should confirm and deliberately does not -- turning it to
the API makes this service read the owner's Claude credential and call Anthropic -- because that
consequence needs to be *visible before the press*, not confirmed after it, so it is spelled
into the row's own label. The two preference rows ask nothing and cost nothing outside this
process, and they alone report a failure as "changed, but not remembered": for them the change
is immediate and only the memory of it can fail.
"""

from __future__ import annotations

import logging

from textual.widgets import OptionList

from remote_agents.adapters.tui.preferences import (
    PROJECT_ORDER_LABELS,
    PROJECT_ORDER_TITLE,
    THEME_LABELS,
    THEME_TITLE,
    next_theme,
    read_project_order,
    read_theme,
)
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import HostRemoteControlConfirmModal

# From the dashboard rather than re-spelled here, and the direction of the import is what keeps
# it honest: the dashboard draws the Codex *reading* on its limits pane, so that module owns the
# six words, the remedy sentences and the longer explanations. A second copy in this file would
# be the second renderer DEC-043 exists to prevent, and the one that would drift is this one --
# it is the screen an owner reaches exactly when the reading is one they cannot act on
# confidently. The word both rows use for a capability nobody wired is not among them: it is
# `UNAVAILABLE` in `application/`, which the dashboard imports too, so the two rows still say it
# by identity rather than by agreement.
from remote_agents.adapters.tui.screens.dashboard import (
    _HOST_AMBIGUOUS_REMEDY,
    _HOST_CONNECTION_EXPLANATIONS,
    host_remote_control_line,
)
from remote_agents.application.host_remote_control import host_remote_control_directions
from remote_agents.application.limits_source import (
    LIMITS_SOURCE_LABELS,
    LIMITS_SOURCE_TITLE,
    next_limits_source,
)
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_TITLE,
    UNAVAILABLE,
    next_remote_control_default,
    remote_control_default_line,
    remote_control_default_word,
)
from remote_agents.domain.remote_control import HostRemoteControlStatus, RemoteControlDefault

_LOG = logging.getLogger(__name__)

#: Stable ids for the rows, so `choose` routes on what the row *is* rather than on the
#: position it happened to be drawn at. A row found by index is a row that acts on the wrong
#: provider the day a third one is added.
_CLAUDE_ROW = "settings:claude-remote-control-default"
_CODEX_ROW = "settings:codex-remote-control"
_LIMITS_SOURCE_ROW = "settings:claude-limits-source"
_THEME_ROW = "settings:theme"
_ORDER_ROW = "settings:project-order"

#: The rows this screen declares, in the order it draws them: the three host subjects first,
#: then the two the terminal keeps for itself.
#:
#: A tuple rather than a count in prose, for the reason `ALL_SCREENS` gives next door -- a
#: numeral written once is a numeral the list grows past in silence. The tests sweep this
#: instead of counting a render, so a row added without being declared here fails rather than
#: quietly changing what "the third row" means to every test that walks the cursor.
SETTINGS_ROWS = (_CLAUDE_ROW, _CODEX_ROW, _LIMITS_SOURCE_ROW, _THEME_ROW, _ORDER_ROW)


def _limits_source_line(value: str | None) -> str:
    """The limits-source row, for a stored choice or for a host that has none to make.

    Private and here rather than in `application/` beside its labels, which is the one place
    this row parts company with the Claude row above it. That line is assembled in
    `application/` because the **bot draws the same line**; this one is not, because the bot's
    `/settings` renders this choice as a *button* carrying its own label and emoji, never as a
    ` · ` row. Putting the assembly in `application/` would be a shared renderer with one
    caller -- a promise of agreement between surfaces that do not in fact draw the same thing.
    What they do share, and what DEC-007 actually asks for, is the words: both reach
    `LIMITS_SOURCE_TITLE` and `LIMITS_SOURCE_LABELS`.

    `None` is *unavailable* rather than the default's word, and the distinction is load-bearing
    on this row more than on any other: a host with no Claude provider cannot choose, and
    rendering that as *status line* would tell the owner their limits come from a hop that is
    not running.
    """
    if value is None:
        return f"{LIMITS_SOURCE_TITLE} · {UNAVAILABLE}"
    return f"{LIMITS_SOURCE_TITLE} · {LIMITS_SOURCE_LABELS.get(value, value)}"


SETTINGS_INSTRUCTION = "Press enter on a row to change it."
"""What this position is for, in the one line the status region holds.

Not "choose a setting": the rows are the settings, and what the owner needs to know is that
Enter *acts* here rather than opening something -- this is the only position in the surface
where a row changes a fact about the machine without navigating anywhere.
"""


class SettingsScreen(ChoiceScreen):
    """Every setting this machine has, in two groups the owner does not have to be told about.

    The first rows name a provider and the state its next session starts in; the last two name
    what this terminal remembers about itself. The grouping is the draw order and nothing more
    -- no headings, no separators -- because the screen is short enough that a heading would be
    a bigger thing to read than the rows it introduced.
    """

    #: Fixed by construction -- every row is drawn whatever its capability answers -- so there
    #: is no emptiness for an empty state to describe (DEC-009's `NEVER_EMPTY` case).
    empty_state = NEVER_EMPTY

    position = "SETTINGS"
    crumb = "Settings"

    #: Every row is a reading of something outside this process -- a provider's settings file,
    #: a daemon, or this surface's own preference file -- so `ctrl+r` means something here: a
    #: file the owner edited with `/config`, or a daemon that came up since the screen was
    #: opened, is exactly what a re-read is for. Declared beside `refresh_contents` because the
    #: footer takes this flag's word for it (`check_action`).
    can_refresh = True

    def __init__(self) -> None:
        super().__init__()
        #: The last reading of each provider row. `None` is both "not read yet" and "no
        #: capability wired", which the two render identically and on purpose: before the first
        #: read there is nothing this surface can honestly claim about the machine either.
        self._claude_default: RemoteControlDefault | None = None
        self._host_status: HostRemoteControlStatus | None = None
        #: The stored limits source, or `None` for a composition that wired no Claude provider.
        #: `None` rather than the default string, because "this host cannot choose" and "this
        #: host chose the hop" are different answers and the row must not render them alike.
        self._limits_source: str | None = None
        #: The two terminal preferences, as last read. Plain strings rather than `None`-able
        #: readings: `read_theme` and `read_project_order` are total and always answer one of
        #: the values this surface knows, so there is no absence for these two rows to render.
        self._theme = ""
        self._project_order = ""

    async def populate(self) -> None:
        self.hide_entry()
        self.set_status(SETTINGS_INSTRUCTION)
        await self._read_every_row()
        self._draw_settings_rows()

    async def refresh_contents(self) -> None:
        """`ctrl+r`: re-read every row and redraw, leaving the cursor where it is."""
        await self._read_every_row()
        self._draw_settings_rows()

    def _read_preferences(self) -> None:
        """Re-read the two rows this surface owns: what is in force, and what was remembered.

        **The theme row reads the theme in force, not the stored one**, and that is the whole
        difference between these two rows and the provider rows above them. For Claude and
        Codex the file *is* the fact -- the provider reads it at its next start, so a write
        that did not land means nothing changed. Here the change is immediate and local: the
        palette can put the app on a theme this file will not store, and after a refused write
        the app is visibly in the new theme. A row drawing the stored value would then say
        *night* on a screen painted in day, which is the one reading that is certainly wrong.

        Synchronous because the file is small and `RemoteAgentsTui.__init__` already reads it
        this way on the event loop; both reads are total and neither can raise.
        """
        theme = str(self.tui.theme)
        self._theme = theme if theme in THEME_LABELS else read_theme(self.services.preferences_path)
        self._project_order = read_project_order(self.services.preferences_path)

    async def _read_every_row(self) -> None:
        """Read each capability, and let neither failure take the other row's reading down.

        Both ports promise never to raise for a read -- the stored default answers
        `PROVIDER_DEFAULT` for every way a file can be unreadable, and the host service answers
        UNREACHABLE rather than raising -- so these `except` clauses are for the shapes they
        cannot promise about: a composition wiring something else, a cancelled read. What is
        already drawn is then stale rather than wrong, which is the contract every background
        read in this surface keeps.
        """
        port = self.services.backend.claude_remote_control_default
        if port is None:
            # A declared absence, not a failure (DEC-061). Left as `None` so the row says
            # "unavailable" rather than keeping a reading from a capability that is gone.
            self._claude_default = None
        else:
            try:
                self._claude_default = await port.read()
            except Exception:
                _LOG.exception("Claude's stored Remote Control default could not be read")
        control = self.services.backend.host_remote_control
        if control is None:
            self._host_status = None
        else:
            try:
                self._host_status = await control.status()
            except Exception:
                _LOG.exception("this host's Remote Control could not be read")
        source = self.services.backend.claude_limits_source
        if source is None:
            # A declared absence, not a failure (DEC-061). Left as `None` so the row says
            # "unavailable" rather than keeping a reading from a capability that is gone.
            self._limits_source = None
        else:
            try:
                self._limits_source = await source.read()
            except Exception:
                _LOG.exception("the Claude limits source could not be read")
        self._read_preferences()

    def _draw_settings_rows(self) -> None:
        """Draw every row, Claude first because it is the provider the host launches most.

        The order is `SETTINGS_ROWS` and the tests sweep that tuple, so a row added to one and
        not the other fails rather than silently renumbering what every cursor-walking test
        thinks it is pressing.

        Enabled rows, unlike the limits pane's: here the row *is* the control, so a cursor
        resting on one and answering Enter with silence would be the broken key that pane's own
        disabled-row comment is about.
        """
        if not self.showing:
            return
        self.show_choices(
            (
                (_CLAUDE_ROW, remote_control_default_line(self._claude_default)),
                (_CODEX_ROW, host_remote_control_line(self._host_status)),
                (_LIMITS_SOURCE_ROW, _limits_source_line(self._limits_source)),
                (_THEME_ROW, f"{THEME_TITLE} · {THEME_LABELS.get(self._theme, self._theme)}"),
                (
                    _ORDER_ROW,
                    f"{PROJECT_ORDER_TITLE} · "
                    f"{PROJECT_ORDER_LABELS.get(self._project_order, self._project_order)}",
                ),
            ),
            highlight=self._highlighted_row(),
        )

    def _highlighted_row(self) -> int:
        """Keep the cursor on the row the owner is acting on across a redraw.

        A press redraws every row, and `show_choices` rests on row 0 by default -- so pressing
        Enter on the Codex row once would move the cursor to the Claude row, and pressing Enter
        again would change a different provider's setting than the one the owner just changed.
        That is the cursor-moved-under-the-press hazard DEC-052 and DEC-062 are about, and it
        got worse rather than better as the screen grew: this is the one position where *every*
        row mutates something, so there is no harmless row for a sprung cursor to land on.
        """
        found = self.query("#choices")
        if not found:
            return 0
        highlighted = found.first(OptionList).highlighted
        return 0 if highlighted is None else highlighted

    async def choose(self, key: str) -> None:
        """Act on the row the owner pressed.

        **This is the screen handler every confirmation in this tree is raised from**, reached
        from `on_option_list_option_selected` on this screen's own message pump -- which is
        what makes the suspension inside `ask_to_confirm` safe (DEC-025). A row that posted to
        a binding instead would be the caller DEC-068 forbids.
        """
        if key == _CLAUDE_ROW:
            await self.advance_claude_remote_control_default()
            return
        if key == _CODEX_ROW:
            await self.confirm_codex_remote_control()
            return
        if key == _LIMITS_SOURCE_ROW:
            await self.advance_limits_source()
            return
        if key == _THEME_ROW:
            await self.advance_theme()
            return
        if key == _ORDER_ROW:
            await self.advance_project_order()

    async def advance_limits_source(self) -> None:
        """One press: read, advance by one, write, read back, say what it now is.

        **The Claude row's shape exactly**, and that is the point rather than an economy: the
        two rows write to different files owned by different programs, but what the owner can
        observe is identical -- one press moves one step, a second returns, and the row after a
        press is drawn from what the file now says rather than from what the press intended.

        **No confirmation, and this is the row where that was worth arguing.** Turning it to
        the API makes the service read the owner's Claude credential and call Anthropic, which
        is the most consequential thing any row on this screen does. It still asks nothing,
        because a confirmation exists to stop an owner changing something they cannot put back,
        and this is one press back. What the consequence needs is to be *visible*, not to be
        confirmed -- so it is spelled into the label itself (`LIMITS_SOURCE_LABELS`), where the
        owner reads it before pressing rather than in a modal after.

        **A refused write is reported as a refusal, detected by the read-back.** `write` cannot
        raise to say it declined -- and `write_limits_key` declines more shapes than any other
        writer here, every one of them something an owner can produce by hand-editing their own
        `config.toml` -- so the only signal on this side is that the re-read is not what was
        asked for.
        """
        port = self.services.backend.claude_limits_source
        if port is None or self.tui.busy:
            # A dead-end press is worse than an absent row: the row says "unavailable" and the
            # key does nothing, rather than reporting a change nothing could have made.
            return
        async with self.holding_the_guard():
            try:
                async with self.awaiting(f"Changing {LIMITS_SOURCE_TITLE}…"):
                    intended = next_limits_source(await port.read())
                    await port.write(intended)
                    # Read back rather than drawing `intended`: the write may have been
                    # refused, and the file is the only thing that knows.
                    self._limits_source = await port.read()
            except Exception as error:
                _LOG.exception("the Claude limits source could not be changed")
                # Not "was not changed": the write may have landed and only the read-back
                # failed, which would make that sentence the false half of a true-sounding pair.
                self.announce(
                    f"{LIMITS_SOURCE_TITLE} could not be confirmed: {error} "
                    "Reopen Settings to see what it says."
                )
                return
            if not self.showing:
                return
            self._draw_settings_rows()
            word = LIMITS_SOURCE_LABELS.get(self._limits_source, self._limits_source)
            if self._limits_source == intended:
                self.set_status(f"{LIMITS_SOURCE_TITLE} is now {word}.")
            else:
                self.announce(f"{LIMITS_SOURCE_TITLE} could not be changed; it is still {word}.")

    async def advance_theme(self) -> None:
        """One press: move to the other relay theme, and say whether it will be remembered.

        **No `awaiting` cover and no port.** The two rows above ask something outside this
        process and can be kept waiting; this one assigns a reactive and writes a small file
        on the way out of the signal, so a spinner would be drawn and removed inside one
        frame. The guard is still held, because a press that redraws the row must not be able
        to interleave with the Codex row's modal.

        **The change is never in doubt; only the memory of it is.** `write_theme` is total and
        a host that wired no preferences path stores nothing at all -- so the sentence on a
        failure is *"is now day, but it could not be remembered"* and not the Claude row's
        *"could not be changed; it is still off"*. Saying the latter here would be false twice
        over: the theme did change, and the owner can see that it did.
        """
        if self.tui.busy:
            return
        async with self.holding_the_guard():
            intended = next_theme(self._theme)
            # The assignment is the switch. `RemoteAgentsTui` subscribes to Textual's theme
            # signal at mount and `_remember_theme` is what writes -- so this row deliberately
            # does not call `write_theme` itself, or the palette and the row would be two
            # writers of one preference.
            self.tui.theme = intended
            stored = read_theme(self.services.preferences_path)
            self._read_preferences()
            if not self.showing:
                return
            self._draw_settings_rows()
            word = THEME_LABELS.get(intended, intended)
            if stored == intended:
                self.set_status(f"{THEME_TITLE} is now {word}.")
            else:
                self.announce(
                    f"{THEME_TITLE} is now {word}, but it could not be remembered "
                    "and this surface will open in the other one.",
                    severity="warning",
                )

    async def advance_project_order(self) -> None:
        """One press: re-sort the catalogue the other way, and say whether it will be kept.

        The ordering itself belongs to the app -- `switch_project_order` holds both the chosen
        order and the snapshot it applies to, and the projects position re-draws from that on
        its next reveal -- so this row is a second caller of the key `o` already has, not a
        second implementation of it. The refusal wording is `advance_theme`'s, for the same
        reason: the list *is* re-ordered, whatever the file managed to record.
        """
        if self.tui.busy:
            return
        async with self.holding_the_guard():
            chosen = await self.tui.switch_project_order()
            stored = read_project_order(self.services.preferences_path)
            self._read_preferences()
            if not self.showing:
                return
            self._draw_settings_rows()
            word = PROJECT_ORDER_LABELS.get(chosen, chosen)
            if stored == chosen:
                self.set_status(f"{PROJECT_ORDER_TITLE} is now {word}.")
            else:
                self.announce(
                    f"{PROJECT_ORDER_TITLE} is now {word}, but it could not be remembered "
                    "and this surface will open in the other one.",
                    severity="warning",
                )

    async def advance_claude_remote_control_default(self) -> None:
        """One press: read, advance by one, write, read back, say what it now is.

        **No confirmation**, and that is a decision rather than an omission. A confirmation
        exists to stop an owner changing something they cannot put back; this writes one key
        into a file, takes effect at the next `claude` start, and three presses return it to
        where it began. Asking here would be the confirmation fatigue DEC-018 declined to buy.

        **The row is redrawn from a fresh read rather than from the value written.** Writing
        `PROVIDER_DEFAULT` *removes* the key instead of storing a third value, and a failed
        write is one log line rather than an exception -- so what the file now says is the only
        honest thing to draw, and it is the one thing a surface drawing its own intention would
        get wrong exactly when it mattered.

        **A refused write is reported as a refusal, and it is detected by comparing what the file
        now says against what the press intended.** An earlier version of this docstring claimed
        the status line solved that on its own -- *"a row that changed and a row that refused look
        identical to someone who has just pressed a key"* -- and it did not: the line named
        whatever the file held, so a refused advance to `on` produced *"is now on"*, which is
        character-for-character what a successful advance to `on` produces. The port cannot raise
        to say so (a `HookInstallError` from an unreproducible settings file is one log line by
        contract), so the only signal available on this side is that the re-read is not the value
        asked for. Found by the Stage 3 gate's adversarial review.
        """
        port = self.services.backend.claude_remote_control_default
        if port is None or self.tui.busy:
            # A dead-end press is worse than an absent row: the row says "unavailable" and the
            # key does nothing, rather than reporting a change nothing could have made.
            return
        async with self.holding_the_guard():
            try:
                async with self.awaiting(f"Changing {REMOTE_CONTROL_DEFAULT_TITLE}…"):
                    intended = next_remote_control_default(await port.read())
                    await port.write(intended)
                    # Read back rather than drawing `intended`. The write may have been refused,
                    # and `PROVIDER_DEFAULT` is stored as the key's *absence*, so the file is the
                    # only thing that knows.
                    self._claude_default = await port.read()
            except Exception as error:
                _LOG.exception("Claude's stored Remote Control default could not be changed")
                # Not "was not changed": the write may have landed and only the read-back failed,
                # in which case that sentence would be the false half of a true-sounding pair.
                self.announce(
                    f"{REMOTE_CONTROL_DEFAULT_TITLE} could not be confirmed: {error} "
                    "Reopen Settings to see what it says."
                )
                return
            if not self.showing:
                return
            self._draw_settings_rows()
            word = remote_control_default_word(self._claude_default)
            if self._claude_default is intended:
                self.set_status(f"{REMOTE_CONTROL_DEFAULT_TITLE} is now {word}.")
            else:
                self.announce(
                    f"{REMOTE_CONTROL_DEFAULT_TITLE} could not be changed; it is still {word}."
                )

    async def confirm_codex_remote_control(self) -> None:
        """Re-read the daemon, offer the direction its reading opens, and issue only on `True`.

        Shaped after `DashboardScreen.confirm_host_remote_control` and reaching the same policy,
        the same modal and the same one command issuer, so the two positions cannot disagree
        about what a press does. What is not shared is the body: the dashboard's version belongs
        to a key on a pane that draws the line, this one belongs to a row, and the plan's scope
        for this task is the new screen rather than a refactor of the one that shipped.

        The guard is held across the re-read *and* the whole modal, so an escape landing
        mid-read cannot pop this screen out from under a question already on its way, and it is
        released before the call that takes the guard itself.

        **The direction comes from the fresh read, not from the drawn row**, which may be as
        old as the last time this screen was opened. Where the policy opens *two* directions --
        ERRORED and UNREACHABLE, the two readings that mean "we do not know" -- this asks which
        rather than guessing, and announces the reading and its remedy first, because an owner
        pressing a button that cannot explain itself is the failure this whole line exists to
        end.
        """
        control = self.services.backend.host_remote_control
        if control is None or self.tui.busy:
            return
        async with self.holding_the_guard():
            await self._read_every_row()
            if not self.showing:
                return
            self._draw_settings_rows()
            status = self._host_status
            directions = host_remote_control_directions(status)
            if not directions:
                return
            chosen = directions[0]
            if len(directions) > 1:
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

    def show_host_remote_control(self, status: HostRemoteControlStatus | None) -> None:
        """Draw a reading this screen did not read itself, and name it in words.

        The one caller is `RemoteAgentsTui.set_host_remote_control`, which has just been handed
        the daemon's own reading of the change it made -- so asking again would be a second
        round trip that could only disagree with the one just made. Named the same as
        `LimitsRegion`'s method of the same job, because the app drives both positions through
        it and a second name would be a second thing for that one caller to know about.
        """
        self._host_status = status
        self._draw_settings_rows()
        self.set_status(f"{host_remote_control_line(status)}.")
