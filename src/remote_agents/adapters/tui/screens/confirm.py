"""The destructive confirmations, as modals that must be answered before anything else.

Stage 2 gave these two positions a `Screen` each so `Step` could be deleted. That was the
mechanical half. This is the half DEC-007 actually asks for: a `ModalScreen[bool]`, awaited
through `push_screen_wait`, which is what makes an unanswered confirmation impossible to walk
away from.

**What "modal" buys, precisely.** `ModalScreen` sets `_modal = True`, and `Screen`'s
`_modal_binding_chain` truncates the binding chain at the first modal node
(`textual/screen.py`), so while one of these is on top the app's own `BINDINGS` — `ctrl+s`,
`ctrl+n`, `ctrl+o`, `ctrl+r`, `escape`, `ctrl+q` — are not in the chain at all. As ordinary
screens they *were*: `ctrl+s` on the force confirmation ran `action_sessions`, which unwound
the stack to the sessions list, and the caller that had asked the question never learned the
answer. That is the gap between "the abort is highlighted" and "the question was answered",
and it is the one a second surface with destructive power cannot be given.

The abort is still **first**, so it is the row the cursor rests on and the row a stray enter
activates. Confirming means deliberately moving to a different row. That mitigation predates
the modal and is unchanged by it — `initial_focus_is_mutating` is what the gate sweeps every
registered confirm for, and `ALL_CONFIRMS` is what makes a third one added later fail that
sweep rather than ship without it.

**Both confirmations are modals now, and the Remote Control one changed shape to get here.**
It used to be a three-row screen — Cancel, Enable, Disable — which is a *chooser*, not a
confirmation: the direction was still undecided when the question was asked. The direction is
chosen on the session detail now and this modal confirms exactly one of them, which is both
what makes it answerable with a `bool` and what the bot has always done
(`telegram/service.py`, `_detail_reply`).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.model import _CANCEL
from remote_agents.application.session_actions import explain_state
from remote_agents.domain.models import SessionRecord
from remote_agents.domain.remote_control import RemoteControlState


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
        choices = self.query_one("#choices", OptionList)
        choices.highlighted = self.resting_index
        choices.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Answer with the row that was chosen, and with `False` for anything unrecognized."""
        event.stop()
        self.dismiss(event.option_id == self.confirm_key)

    def action_abort(self) -> None:
        """Escape answers the question rather than leaving it unanswered.

        Bound on the modal because the app's own `escape` binding cannot reach here — that is
        the whole point of the modal — so without this the owner would have no key that
        closes it. Answering `False` rather than popping raw keeps every exit routed through
        the one result the caller awaits.
        """
        self.dismiss(False)


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
            f"{explain_state(record.state)}"
        )


class RemoteControlConfirmModal(ConfirmScreen):
    """Ask before changing a live pane's control mode, with Cancel as the resting row.

    Which direction is being confirmed is chosen on the session detail and carried here in
    the question, rather than being a second decision taken inside the confirmation. That is
    what lets this be the same `ModalScreen[bool]` as the force confirm — and it is also what
    the bot already does (`telegram/service.py`, `_detail_reply`), where the detail offers
    "Enable Remote Control" and "Disable Remote Control" as separate buttons and the confirm
    that follows asks about exactly one of them.
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


#: Every confirm the surface can put in front of a destructive action. The stage gate sweeps
#: it for a resting row that mutates, and `tests/unit/adapters/tui/test_confirm_modals.py`
#: parametrizes over it — so a third destructive confirm added later without these
#: guarantees fails here rather than shipping.
ALL_CONFIRMS = (ForceConfirmModal, RemoteControlConfirmModal)
