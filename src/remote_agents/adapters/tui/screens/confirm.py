"""The destructive confirmations, as modals that must be answered before anything else.

Stage 2 gave these two positions a `Screen` each so `Step` could be deleted. That was the
mechanical half. This is the half DEC-007 actually asks for: a `ModalScreen[bool]`, awaited
through `push_screen_wait`, which is what makes an unanswered confirmation impossible to walk
away from.

**What "modal" buys, precisely.** `ModalScreen` sets `_modal = True`, and `Screen`'s
`_modal_binding_chain` truncates the binding chain at the first modal node
(`textual/screen.py`), so while one of these is on top the app's own `BINDINGS` — `ctrl+s`,
`ctrl+n`, `ctrl+o`, `ctrl+r`, `escape` — are not in the chain at all.

**That truncation covers ordinary bindings only, and two things escaped it.**
`App._check_bindings(key, priority=True)` walks the *untruncated* chain, so Textual's own
command palette (`ctrl+p`) did open over a confirmation — harmlessly, since the question
stayed open beneath it and quitting from it resolved the answer to no. **It no longer opens
at all**, and not by design: Sub-plan 3 made `RemoteAgentsTui.check_action` delegate to the
active screen so the footer could stop advertising keys that do nothing, and `run_action`
consults `check_action` before dispatching — so the palette now asks this class, which answers
`False` to everything but its own abort. A paragraph describing the old escape route survived
the change that closed it; re-verified by driving `run_action("command_palette")` against an
open modal and watching it refuse.

`ctrl+q` is the second, and it does not quit under a confirmation for a reason worth
writing down, because it is not this one: `App.BINDINGS` declares `ctrl+q` with `priority=True`, and
`DOMNode._merge_bindings` **replaces** a key's bindings per most-derived class rather than
extending them — so `RemoteAgentsTui.BINDINGS`' own non-priority `ctrl+q` strips the priority
off it and it falls inside the truncated chain. Deleting that line as redundant with Textual's
default would bring the priority binding back and let the app be quit out from under an open
kill confirmation. It resolves to no, so nothing is killed; the line is load-bearing anyway.
As ordinary
screens they *were*: `ctrl+s` on the force confirmation ran `action_sessions`, which unwound
the stack to the sessions list, and the caller that had asked the question never learned the
answer. That is the gap between "the abort is highlighted" and "the question was answered",
and it is the one a second surface with destructive power cannot be given.

The abort is still **first**, so it is the row the cursor rests on and the row a stray enter
activates. Confirming means deliberately moving to a different row. That mitigation predates
the modal and is unchanged by it — `initial_focus_is_mutating` is what the gate sweeps every
registered confirm for, and `ALL_CONFIRMS` is what makes a third one added later fail that
sweep rather than ship without it.

**A confirmation is only ever asked from a screen handler, and that is a rule rather than an
accident (DEC-025).**

If you are about to `await ask_to_confirm(...)` from anywhere that is not a screen's own
handler — a worker, a timer, a background task, a message pump callback, a global binding, **or
a screen's own binding action** — stop. That is the caller this paragraph exists to warn you
about.

**A screen binding is on that list and was not, and the omission cost an outage** (DEC-068). A
binding declared on a screen looks like the screen's own code and is not: Textual dispatches a
non-priority binding from `App._on_key` -> `_check_bindings` -> `run_action`, so it runs on the
**App's** pump. Suspending there suspends the application. What to do instead is what
`RowStopAction` and `OpeningAction` do — post a message to your own screen and raise the modal
from that handler.

Here is what will happen. `ask_to_confirm` pushes a modal and suspends your coroutine until
the modal answers. Nothing guarantees the modal is answered: it can be popped for reasons that
have nothing to do with the owner's decision — a navigation that unwinds the stack, an error
path that resets the screen, a second entry point arriving mid-flight. When that happens your
`await` is never satisfied and never fails. It simply waits, holding whatever your caller was
holding.

**This has bitten, once, and the sentence here used to say it never had.** On 2026-08-30 a
force confirmation was raised from a screen *binding* on the sessions list: the modal drew
correctly and the application then stopped answering entirely — Escape did nothing, `ctrl+q`
did nothing, and the process had to be killed. The protection is not that the code prevents it.
It is that every confirmation asked from a screen *handler* runs on that screen's pump, so
while it is suspended the pump is not delivering the events that would pop the modal out from
under it. **The protection is a side effect of where the call is made from.** Move one call off
the screen's pump and it is gone, silently.

DEC-008 is why the obvious guard is absent: a destructive action deliberately does not pass
`exclusive`, because a repeat must be dropped rather than cancel the action already in flight.
That choice is right and is not being revisited. It does mean nothing in the framework will
catch this for you — and DEC-025 declined a timeout on purpose, because a timed-out force-stop
confirmation can neither proceed (nobody confirmed) nor cancel (the owner may be mid-decision),
which would replace a hang nobody has hit with an ambiguity everybody would.

What catches *some* of it is
`tests/architecture/test_confirmations_are_asked_from_screen_handlers.py`, which sweeps this
package for any lexical path from one of those forbidden callers to `ask_to_confirm`. That is a
check on the *rule*, not a guard on the *runtime*: it fails the suite when someone writes the
bad caller, which is the one thing a paragraph on its own cannot do. It does not make the hang
unreachable, and DEC-025 is explicit that it stays unreachable by convention.

**It cannot catch the screen-binding case, and saying so is the point.** That sweep asserts the
caller is a method on a class whose name ends in `Screen` — and the method that froze the app
was one. Which pump a call arrives on is a property of its dynamic context, not of its lexical
site, so no static check of call sites can decide it. The sweep still earns its place for
workers, timers and app-level bindings; DEC-068 records the gap it leaves.

**Both confirmations are modals now, and the Remote Control one changed shape to get here.**
It used to be a three-row screen — Cancel, Enable, Disable — which is a *chooser*, not a
confirmation: the direction was still undecided when the question was asked. The direction is
chosen on the session detail now and this modal confirms exactly one of them, which is both
what makes it answerable with a `bool` and what the bot has always done
(`telegram/service.py`, `_detail_reply`).
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.model import _CANCEL
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
)
from remote_agents.application.session_actions import explain_state
from remote_agents.domain.models import SessionRecord
from remote_agents.domain.remote_control import PairingCode, RemoteControlState


class ConfirmScreen(ModalScreen[bool]):
    """One irreversible question, answered yes or no, with the abort under the cursor.

    Dismissed with `True` only by choosing the confirm row. Every other way out — the abort
    row, escape, an unrecognized key — dismisses with `False`, so "the owner left" and "the
    owner said no" are the same answer and neither can be mistaken for consent.

    The question and the confirm label are constructor arguments with class-level defaults
    rather than required ones, because the registry sweep in the stage gate constructs each
    registered confirm with no arguments to ask whether its resting row mutates. A confirm
    that could only be built with a live `SessionRecord` would have to be excluded from that
    sweep, and an excluded confirm is exactly the one that ships without the mitigation.
    """

    #: The name this modal is committed under in the snapshot baselines, as every screen
    #: declares one. Deliberately not the two names the deleted `Step` members carried: the
    #: stage gate greps those literals out of the source and the baselines to prove the step
    #: machine's confirmations are gone, and a sweep that has to carve out prose exceptions
    #: stops being run — so they are described here rather than spelled.
    position = ""
    #: Shown above the rows. Overridden per instance by the screen that opens it.
    question = ""
    #: The row that answers `True`. Never first, and never the resting row.
    confirm_key = ""
    confirm_label = ""
    abort_label = "Cancel"

    BINDINGS = [Binding("escape", "abort", "Cancel")]

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen #dialog {
        width: 80%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfirmScreen #status { height: auto; padding: 0 0 1 0; }
    ConfirmScreen #choices { height: auto; }
    """

    def __init__(self, question: str = "", confirm_label: str = "") -> None:
        super().__init__()
        self._question = question or self.question
        self._confirm_label = confirm_label or self.confirm_label
        # A question is answered once. See `_answer` for what the second answer did.
        self._answered = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            # `markup=False` for the same reason it is set on the body every other position
            # renders: the question interpolates `record.display.rendered`, which carries the
            # owner's own free-text label, and an unbalanced bracket there raised `MarkupError`
            # — taking down the screen that was asking whether to kill an agent.
            yield Static(self._question, id="status", markup=False)
            # Same ids as the shared body deliberately: `#status` and `#choices` are the
            # vocabulary every test helper and the resting-cursor assertions already read, so
            # a modal that renamed them would be invisible to the checks that matter most here.
            yield OptionList(
                *(Option(label, id=key) for key, label in self.rows), id="choices", markup=False
            )

    @property
    def rows(self) -> tuple[tuple[str, str], ...]:
        """The choices, abort first. Overridden by a confirm offering more than one answer."""
        return ((_CANCEL, self.abort_label), (self.confirm_key, self._confirm_label))

    #: Where the cursor opens. Index 0 is the abort by construction of `rows`.
    resting_index = 0

    @property
    def initial_focus_is_mutating(self) -> bool:
        """Whether the row the cursor opens on is one that changes anything.

        Asked of the class rather than of a mounted screen so the gate can sweep every
        registered confirm without driving the surface to each one — a check that has to be
        navigated to is a check that covers whatever the navigation happens to reach.
        """
        return self.rows[self.resting_index][0] != _CANCEL

    def on_mount(self) -> None:
        """Rest the cursor on the abort, once, at mount.

        Deliberately **not** through `ChoiceScreen.show_choices` and `_rest_cursor`, which is
        where DEC-007's third mitigation ("every screen resets the cursor to a non-mutating
        entry") actually lives. That machinery exists for a screen that is *refilled*: it
        carries a generation counter so a deferred placement computed against one fill stands
        down when a later fill supersedes it, and it re-asserts the highlight after a refresh
        so a resting row below the fold is scrolled into view.

        Neither applies here, and both would be dead weight. A confirmation is built fresh per
        question, its rows are static, it is never refilled, and it is two rows — there is no
        second fill to supersede this one and nothing to scroll. **A subclass that starts
        refilling its rows has to adopt `_rest_cursor`'s generation guard**, because that is
        the moment the invariant this relies on stops holding.
        """
        choices = self.query_one("#choices", OptionList)
        choices.highlighted = self.resting_index
        choices.focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide every app-level binding, which is what is already true here.

        Answered explicitly rather than inherited, and it changes nothing at runtime: a modal
        truncates the binding chain, so none of the app's actions can reach this screen to be
        checked in the first place. What it buys is that the stage gate's sweep — "no screen
        inherits the permissive default" — gets a real answer from these two rather than
        passing them over, and that a reader of this class does not have to reconstruct the
        truncation argument to know what the footer shows.

        The modal's *own* bindings are unaffected: `check_action` is consulted for the
        namespace an action resolves in, and `abort` is this screen's.
        """
        return True if action == "abort" else False

    def _answer(self, confirmed: bool) -> None:
        """Deliver the answer, and refuse to deliver a second one.

        `Screen.dismiss` calls the top result callback and then `app.pop_screen()`
        *unconditionally* — and `pop_screen` pops `_screen_stack[-1]`, which is not
        necessarily this screen. The first dismiss also consumes the result callback, so a
        second one finds none, raises nothing, and quietly pops **whatever is on top now**.

        That made a burst of Enter keys walk back down the stack: two answered the question
        and popped the session detail, three took the sessions list with it, and four raised
        `ScreenStackError` out of a message handler and killed the app. It needs no unusual
        input — key autorepeat on the dialog, a double-tap, or buffered stdin over a laggy
        link all deliver several key events in one pump turn, which is exactly the shape the
        pilot's own `press()` does not produce, because it waits for idle between keys. That
        is why the suite could not see it and an adversarial pass could.

        Consent was never inverted by it: the answer delivered is the first one, and the first
        one lands on the abort. What it broke was everything after — a force stop whose
        failure report was then written to a screen no longer on top, so the owner was
        deposited on the sessions list and never told the kill had failed.
        """
        if self._answered:
            return
        self._answered = True
        self.dismiss(confirmed)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Answer with the row that was chosen, and with `False` for anything unrecognized."""
        event.stop()
        self._answer(event.option_id == self.confirm_key)

    def action_abort(self) -> None:
        """Escape answers the question rather than leaving it unanswered.

        Bound on the modal because the app's own `escape` binding cannot reach here — that is
        the whole point of the modal — so without this the owner would have no key that
        closes it. Answering `False` rather than popping raw keeps every exit routed through
        the one result the caller awaits.
        """
        self._answer(False)


class ForceConfirmModal(ConfirmScreen):
    """Ask a second time before killing a running agent.

    Force cannot be undone, so it is deliberately not reachable by repeating whatever
    keystroke opened the detail: the abort is first and highlighted, and confirming means
    moving to a different row on purpose.
    """

    position = "FORCE_MODAL"
    question = "Force stop this session?"
    confirm_key = "force-confirm"
    confirm_label = "Yes, force stop it"

    @classmethod
    def for_record(cls, record: SessionRecord) -> ForceConfirmModal:
        """Build the question this record deserves, in one place both callers share.

        A classmethod rather than a longer `__init__` so the snapshot baseline and the
        session detail render the identical string: a confirmation whose *visual* net shows
        different wording from the one the owner is asked is a net over the wrong screen.
        """
        return cls(
            f"Force stop {record.display.rendered}?\n"
            "This kills the agent immediately and cannot be undone. Any work it has not "
            "saved is lost.\n"
            f"{explain_state(record.state, record.orphan_provenance)}"
        )


class RemoteControlConfirmModal(ConfirmScreen):
    """Ask before changing a live pane's control mode, with Cancel as the resting row.

    Which direction is being confirmed is chosen on the session detail and carried here in
    the question, rather than being a second decision taken inside the confirmation. That is
    what lets this be the same `ModalScreen[bool]` as the force confirm — and it is what the
    bot does too, from the same shared policy: `remote_control_directions` decides which
    directions each detail screen offers, and the confirmation that follows asks about
    exactly one of them. Since that policy reads the last observed state, the usual case is
    a single row on the detail and this modal confirming it; both rows appear only while
    nothing has been observed yet.
    """

    position = "REMOTE_CONTROL_MODAL"
    question = "Change Remote Control for this session?"
    confirm_key = "remote-control-confirm"
    confirm_label = "Yes, change it"

    @classmethod
    def for_change(
        cls, record: SessionRecord, desired: RemoteControlState
    ) -> RemoteControlConfirmModal:
        """The question for one direction, named in both the prompt and the confirm row.

        Naming the direction on the row as well as in the prompt is deliberate: the row is
        what the owner reads while the cursor is next to it, and a generic "Yes" would make
        the two directions indistinguishable at the moment of the decision.
        """
        action = "Enable" if desired is RemoteControlState.ACTIVE else "Disable"
        effect = (
            "Enabling lets this session be driven remotely."
            if desired is RemoteControlState.ACTIVE
            else "Disabling returns it to local control only."
        )
        return cls(
            f"{action} Claude Remote Control for {record.display.rendered}?\n{effect}",
            confirm_label=f"Yes, {action.casefold()} it",
        )


class HostRemoteControlConfirmModal(ConfirmScreen):
    """Ask before changing **this machine's** host Remote Control, not a pane's.

    The sibling above confirms a direction for one live Claude pane. This confirms a
    direction for the host: there is no session in the question, because there is no session
    in the subject. That is why it is a separate class rather than the same one with an
    optional record — a record that is sometimes meaningful is a field every reader has to
    ask about, which is the same argument `HostRemoteControlCommand` makes for itself.

    **The title is the application's, and so is each direction's word.** `HOST_REMOTE_CONTROL_TITLE`
    names the provider on purpose: an owner who already knows the Claude pane toggle would
    otherwise read a bare title as that one and act on the wrong machine-versus-pane
    assumption. `HOST_REMOTE_CONTROL_LABELS` **is** the pane toggle's table by identity, so
    restating either here would recreate exactly the drift DEC-007 ended.

    **Deliberately not in `ALL_CONFIRMS`.** Every arrangement in
    `tests/unit/adapters/tui/test_confirm_modals.py` is keyed by a session-detail row and a
    policy read against a `SessionRecord`, and this modal has neither, so registering it
    would mean inventing an arrangement that describes nothing. The one guarantee that sweep
    exists to hold — the abort resting under the cursor — is inherited from `ConfirmScreen`
    by construction and asserted directly in
    `tests/unit/adapters/tui/test_tui_host_remote_control.py`.
    """

    position = "HOST_REMOTE_CONTROL_MODAL"
    question = f"Change {HOST_REMOTE_CONTROL_TITLE} for this machine?"
    confirm_key = "host-remote-control-confirm"
    confirm_label = "Yes, change it"

    @classmethod
    def for_direction(cls, desired: RemoteControlState) -> HostRemoteControlConfirmModal:
        """The question for one direction, named in both the prompt and the confirm row.

        Naming it on the row as well as in the prompt is `for_change`'s reasoning applied to
        a wider blast radius: the row is what the owner reads while the cursor is next to it,
        and this one does not change what a single agent can be driven from — it changes
        whether the phone can reach *every* Codex session on this host.
        """
        label = HOST_REMOTE_CONTROL_LABELS[desired]
        effect = (
            # The launch-order rule, in the one place an owner is guaranteed to read it. A
            # managed codex pane started while the daemon is down is embedded and stays
            # invisible to the phone for its whole life; one started after lives in the
            # daemon. Saying "every Codex session" here -- which this line used to -- promises
            # the owner something that is false for every pane already running.
            "Codex sessions you start after this can be driven from your phone. "
            "Ones already running cannot; restart them if you need them reachable."
            if desired is RemoteControlState.ACTIVE
            # Third wording, and the first one measured rather than reasoned. It said
            # "sessions already running keep going" (false), then "may be disconnected when
            # it restarts" (overstated). Running the drill on a standalone install settled
            # it: the daemon IS replaced, the attached pane is NOT killed -- same pid, live
            # prompt -- but it comes back reporting "This conversation is unavailable", and
            # that is durable. The cost is the conversation, not the session. DEC-071.
            else (
                "This machine stops answering the phone. The daemon restarts, so an "
                "attached codex session loses the conversation it is in; the pane itself "
                "survives."
            )
        )
        return cls(
            f"{HOST_REMOTE_CONTROL_TITLE}: {label}?\n"
            "This applies to the whole machine, not to one session.\n"
            f"{effect}",
            confirm_label=f"Yes, {label.casefold()}",
        )


class HostRemoteControlDirectionModal(ModalScreen[RemoteControlState | None]):
    """Ask *which* direction, where the policy declines to say which way the host is set.

    `ERRORED` and `UNREACHABLE` both mean "we could not read the setting", so the policy
    offers both directions rather than guessing -- and a surface that then guessed on its
    behalf would put the guess back one layer down. The bot renders both as buttons; this is
    the terminal's equal, and it exists because the two surfaces must offer the same set
    (DEC-007), not because the terminal wanted another screen. The parity contract is what
    found the gap: the terminal used to decline here, and declining is a smaller set.

    It chooses; it does not act. The direction it returns goes on to the ordinary
    confirmation, which is what keeps "the toggle is confirmed" true on both surfaces
    however many directions were open -- the same two steps the bot takes.

    **Not in `ALL_CONFIRMS`, and no `position`.** That registry is swept for a resting row
    that mutates, and nothing here mutates: this screen returns a choice and the confirmation
    after it is what stands in front of the change.
    """

    position = ""
    abort_label = "Cancel"

    BINDINGS = [Binding("escape", "dismiss_direction", "Cancel", show=False)]

    def __init__(self, directions: tuple[RemoteControlState, ...], explanation: str = "") -> None:
        super().__init__()
        self._directions = directions
        # The reading's own sentence, passed in rather than derived here: this class knows
        # about directions, and which reading produced them is the dashboard's fact. It used
        # to say "this machine's setting could not be read" for every case, which is wrong
        # for ERRORED -- there the daemon answered, and what it said was that its own link is
        # broken. Telling an owner their setting is unreadable when the daemon just told them
        # it is enrolled is the sharper end of the same conflation this whole feature exists
        # to stop making.
        self._explanation = explanation

    def compose(self) -> ComposeResult:
        # `#dialog`, `#status` and `#choices` deliberately, not names of this modal's own:
        # that trio is the vocabulary every test helper and the resting-cursor assertion
        # already read, and a modal that renamed them would be invisible to the checks that
        # matter most here. The note on `ConfirmScreen.compose` says so in those words.
        with Vertical(id="dialog"):
            explained = self._explanation or "This machine's setting could not be read."
            yield Static(
                f"{HOST_REMOTE_CONTROL_TITLE}\n{explained}\nChoose the direction to assert.",
                id="status",
                markup=False,
            )
            # `_CANCEL` is the sentinel *id*, not a label -- passing it as the prompt too
            # renders its null byte on screen. `ConfirmScreen` keeps the two apart in `rows`
            # and so does this.
            options = [Option(self.abort_label, id=_CANCEL)] + [
                Option(HOST_REMOTE_CONTROL_LABELS[direction], id=direction.value)
                for direction in self._directions
            ]
            # Cancel first, so leaving and declining rest under the same cursor as everywhere
            # else in this file.
            yield OptionList(*options, id="choices", markup=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = event.option.id
        if chosen is None or chosen == _CANCEL:
            self.dismiss(None)
            return
        self.dismiss(RemoteControlState(chosen))

    def action_dismiss_direction(self) -> None:
        self.dismiss(None)


class HostPairingCodeModal(ModalScreen[None]):
    """Show a pairing code once, then take it off the screen for good.

    **The only screen in this surface that renders a secret**, and every difference from the
    confirms above follows from that one fact.

    *It asks nothing*, so it is `ModalScreen[None]` rather than `[bool]`. There is no yes and
    no no -- the code is either read or it is not -- and giving it an abort row would suggest
    a decision the owner does not have. **Any key dismisses it**, which is the honest
    affordance for a notice: an owner who has copied the code presses whatever is under their
    hand, and one who has not simply does not press.

    *It declares no `position`*, so the snapshot suite cannot commit a picture of it. That is
    not an oversight to be tidied up later: a baseline holding a rendered pairing code would
    be a secret checked into the repository, and a test in
    `tests/unit/adapters/tui/test_tui_host_remote_control.py` asserts this class stays out of
    the baselines so that adding one fails rather than quietly landing.

    *It is not in `ALL_CONFIRMS`* for a simpler reason than its sibling's: that registry is
    swept for a resting row that mutates, and this modal has no rows at all.

    *It is not re-openable.* The code is held for the life of the screen and nothing stores
    it (DEC-013), so once dismissed there is no history entry, no scrollback and no command
    that can bring it back -- the owner mints a new one, which is what the relay expects
    anyway since the old one is still live until it expires.
    """

    BINDINGS = [
        Binding("escape", "dismiss_code", "Dismiss", show=False),
        # `ctrl+p` is Textual's command palette and it is a *priority* binding, so it is
        # dispatched before this screen's `on_key` and the "any key dismisses it" defence
        # never runs. The palette then opens over a modal that is still holding a live
        # pairing code, and one of its own commands is Save Screenshot -- which writes an SVG
        # of the visible screen to disk, code included. That is DEC-013 broken by a
        # first-party affordance. Bound at priority here so the palette key dismisses the
        # code instead of photographing it.
        Binding("ctrl+p", "dismiss_code", "Dismiss", show=False, priority=True),
    ]

    def __init__(self, code: PairingCode) -> None:
        super().__init__()
        # Held on the instance and never on the screen's own state beyond it. The value is
        # read back by `rendered_code()` for the test that proves it reached the screen; it
        # is deliberately not exposed through a property that survives dismissal.
        self._code: PairingCode | None = code

    def compose(self) -> ComposeResult:
        # `markup=False` like every sibling widget in this file, and for the recorded
        # reason: a bracket in text this surface did not author raises `MarkupError` and
        # takes the screen down. The upstream validator admits any printable code, and `[`
        # is printable -- so a code containing one would crash the one screen whose whole
        # job is to show it.
        yield Static(self.rendered_code(), id="host-pairing-code", markup=False)

    def rendered_code(self) -> str:
        """What the owner sees: the code, when it dies, and that it will not be shown again.

        `expires_at` is rendered in the owner's own timezone rather than UTC -- a code with a
        deadline is only useful if the deadline is in the clock they are reading.
        """
        if self._code is None:
            return "This pairing code has been dismissed."
        expires = self._code.expires_at.astimezone().strftime("%H:%M:%S %Z")
        return (
            f"{HOST_REMOTE_CONTROL_TITLE} pairing code\n\n"
            f"    {self._code.code}\n\n"
            f"It expires at {expires}, and this is the only time it is shown.\n"
            "Type it into the ChatGPT app's manual pairing screen.\n"
            "Anyone who has it can drive this machine until it expires.\n\n"
            "Press any key to dismiss."
        )

    def action_dismiss_code(self) -> None:
        self._forget()
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        """Any key at all, because this is a notice and not a question."""
        event.stop()
        self._forget()
        self.dismiss(None)

    def _forget(self) -> None:
        """Drop the value on the way out, rather than trusting nothing else holds it.

        The docstring above rests "dismissed is gone" on no other reference existing. That
        is true today and it is a claim about the whole process rather than about this
        object; clearing the field makes it a property of this object instead, independent
        of when a collector happens to run or what Textual's screen stack retains.
        """
        self._code = None


#: Every confirm the surface can put in front of a destructive action. The stage gate sweeps
#: it for a resting row that mutates, and `tests/unit/adapters/tui/test_confirm_modals.py`
#: parametrizes over it — so a third destructive confirm added later without these
#: guarantees fails here rather than shipping.
#:
#: **Not every confirmation in the surface, and the name invites that reading.** What belongs
#: here is a confirmation standing in front of something *destructive*. The resume flow used to
#: carry one and was deliberately absent from this registry — resume creates a session rather
#: than ending one, so none of the guarantees here were the ones it needed — and that reasoning
#: is what eventually removed the screen rather than merely excluding it (DEC-018: a
#: confirmation is applied to both surfaces or neither, and the bot had none). The two stops
#: that are not here — graceful and cleanup — are absent for the same rule, recorded in the
#: Stage 3 handoff: neither surface confirms them, and confirming on one side only would break
#: the parity DEC-007's first mitigation exists to hold.
ALL_CONFIRMS = (ForceConfirmModal, RemoteControlConfirmModal)
