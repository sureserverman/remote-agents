"""Settings: one row per provider for whether its sessions start remote-controlled.

**Why this is a screen of its own rather than a line on an existing pane.** The two facts it
carries have the *machine* as their subject, not a session and not a launch. Claude resolves
`remoteControlAtStartup` from its own settings file at every start, so the value governs every
`claude` session on this host, including ones the owner opens by hand; Codex's enrollment is a
persisted daemon preference that governs every `codex` one. Neither can be hung off a session
row without claiming to be about that session, and the limits pane -- the one region that does
describe the host -- already carries the Codex *reading* and has no room to carry an action per
provider (its rows truncate at 28 columns on an 80-column terminal, measured). So the two sit
here as two rows of one kind, which is the shape the premise check established they are
(`docs/acceptance-2026-09-11-surface-refresh.md` section 8, plan Stage 3).

**One exception to "every `claude` session", and it is a real one until Stage 4 lands.** The
`claude-remote` profile is `claude --remote-control {managed_name}`, and that flag forces Remote
Control on *over* a settings file saying `false` -- measured, section 8 arm G. So a launch on that
profile comes up connected whatever this row says, which is the one direction of wrongness that
matters: the row reads *off* while the pane is on. The profile is retired in Stage 4 (Task 4.4) and
the launch argv then reads this row (Task 4.2); until both land, this row's claim is about `claude`
launches and hand-started sessions rather than about every session on the host. Found by the
Stage 3 gate's evaluator, which was right that the docstring overstated its reach.

**Every word on both rows is the application's.** `REMOTE_CONTROL_DEFAULT_TITLE` and
`REMOTE_CONTROL_DEFAULT_LABELS` spell the Claude row, `HOST_REMOTE_CONTROL_TITLE` and
`host_remote_control_line` spell the Codex one, and the bot's `/settings` screen renders the
same two rows from the same tables. That is DEC-007's point rather than a tidiness preference:
two surfaces that *agreed* about a wording would be free to stop agreeing, and this row's
vocabulary is the one place in the project where a wrong word is acted on by not acting --
`PROVIDER_DEFAULT` worded as any form of "off" would tell the owner their panes come up
disconnected when the measured behaviour is that they come up connected.

**The two rows are deliberately not symmetrical in what a press costs.** The Claude row writes
a key into a file and the next `claude` start reads it: reversible, three presses back to where
it began, so it asks nothing. The Codex row changes a daemon this machine is enrolled with, so
it confirms -- and its confirmation is raised from `choose`, a screen handler on this screen's
own message pump, never from a binding body (DEC-025 as DEC-068 extends it). An `await` on a
modal from the App's pump stops the surface answering anything at all, quit included, with the
modal drawn correctly because the modal is the last thing it manages to draw.

**Not in `ALL_SCREENS` yet, and that is a gap rather than a decision.** That registry is what
`test_screen_back_paths.py`, `test_binding_visibility.py`, `test_empty_states.py` and the
snapshot suite sweep, and each needs an arrangement or a committed baseline per member -- four
test modules this task's scope does not include. The back path is driven here instead
(`test_escape_returns_to_the_dashboard`); registering the screen and giving it a baseline is
follow-up work, noted at the Stage 3 gate rather than left silent.
"""

from __future__ import annotations

import logging

from textual.widgets import OptionList

from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import HostRemoteControlConfirmModal

# From the dashboard rather than re-spelled here, and the direction of the import is what keeps
# it honest: the dashboard draws the Codex *reading* on its limits pane, so that module owns the
# six words, the remedy sentences and the longer explanations. A second copy in this file would
# be the second renderer DEC-043 exists to prevent, and the one that would drift is this one --
# it is the screen an owner reaches exactly when the reading is one they cannot act on
# confidently. `_HOST_UNAVAILABLE` comes across for the same reason: both rows on this screen
# say the same word for a capability nobody wired, by identity rather than by agreement.
from remote_agents.adapters.tui.screens.dashboard import (
    _HOST_AMBIGUOUS_REMEDY,
    _HOST_CONNECTION_EXPLANATIONS,
    _HOST_UNAVAILABLE,
    host_remote_control_line,
)
from remote_agents.application.host_remote_control import host_remote_control_directions
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_LABELS,
    REMOTE_CONTROL_DEFAULT_TITLE,
    next_remote_control_default,
)
from remote_agents.domain.remote_control import HostRemoteControlStatus, RemoteControlDefault

_LOG = logging.getLogger(__name__)

#: Stable ids for the two rows, so `choose` routes on what the row *is* rather than on the
#: position it happened to be drawn at. A row found by index is a row that acts on the wrong
#: provider the day a third one is added.
_CLAUDE_ROW = "settings:claude-remote-control-default"
_CODEX_ROW = "settings:codex-remote-control"

SETTINGS_INSTRUCTION = "Press enter on a row to change it."
"""What this position is for, in the one line the status region holds.

Not "choose a setting": the rows are the settings, and what the owner needs to know is that
Enter *acts* here rather than opening something -- this is the only position in the surface
where a row changes a fact about the machine without navigating anywhere.
"""


def remote_control_default_word(value: RemoteControlDefault | None) -> str:
    """What the Claude row's state is called -- one word for the row and for the outcome line.

    Split out because the two sentences that need it must not be able to disagree: the row says
    `Claude Remote Control · on` and the status line after a press says `Claude Remote Control
    is now on`, and a second lookup is a second chance for one of them to spell a state the
    other does not.
    """
    return _HOST_UNAVAILABLE if value is None else REMOTE_CONTROL_DEFAULT_LABELS[value]


def remote_control_default_line(value: RemoteControlDefault | None) -> str:
    """The Claude row, for a stored default or for a capability nobody wired.

    Module-level and named, for the reason `host_remote_control_line` is: all four readings can
    then be checked without driving a Textual app to reach each one, and the separator and
    shape stay identical to the row underneath it -- two rows on one screen that formatted
    their values differently would read as two unrelated facts.

    `None` is *unavailable* rather than an omitted row or a guessed state (DEC-009/DEC-061): a
    composition with no Claude provider wired has no default to offer, and a missing row is
    indistinguishable from a surface that forgot to draw one.
    """
    return f"{REMOTE_CONTROL_DEFAULT_TITLE} · {remote_control_default_word(value)}"


class SettingsScreen(ChoiceScreen):
    """Two rows, each naming a provider and the state its next session will start in."""

    #: Fixed by construction -- two providers, two rows, whatever either capability answers --
    #: so there is no emptiness for an empty state to describe (DEC-009's `NEVER_EMPTY` case).
    empty_state = NEVER_EMPTY

    position = "SETTINGS"
    crumb = "Settings"

    #: Both rows are a reading of something outside this process, so `ctrl+r` means something
    #: here: a file the owner edited with `/config`, or a daemon that came up since the screen
    #: was opened, is exactly what a re-read is for. Declared beside `refresh_contents` because
    #: the footer takes this flag's word for it (`check_action`).
    can_refresh = True

    def __init__(self) -> None:
        super().__init__()
        #: The last reading of each row. `None` is both "not read yet" and "no capability
        #: wired", which both rows render identically and on purpose: before the first read
        #: there is nothing this surface can honestly claim about the machine either.
        self._claude_default: RemoteControlDefault | None = None
        self._host_status: HostRemoteControlStatus | None = None

    async def populate(self) -> None:
        self.hide_entry()
        self.set_status(SETTINGS_INSTRUCTION)
        await self._read_both_rows()
        self._draw_settings_rows()

    async def refresh_contents(self) -> None:
        """`ctrl+r`: re-read both capabilities and redraw, leaving the cursor where it is."""
        await self._read_both_rows()
        self._draw_settings_rows()

    async def _read_both_rows(self) -> None:
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

    def _draw_settings_rows(self) -> None:
        """Draw the two rows, Claude first because it is the provider the host launches most.

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
            ),
            highlight=self._highlighted_row(),
        )

    def _highlighted_row(self) -> int:
        """Keep the cursor on the row the owner is acting on across a redraw.

        A press redraws both rows, and `show_choices` rests on row 0 by default -- so pressing
        Enter on the Codex row once would move the cursor to the Claude row, and pressing Enter
        again would change a different provider's setting than the one the owner just changed.
        That is the cursor-moved-under-the-press hazard DEC-052 and DEC-062 are about, in the
        one position where both rows mutate something.
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
            await self._read_both_rows()
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
