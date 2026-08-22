"""What every destructive confirmation must guarantee, asserted once for all of them.

The two confirmations in this surface were pinned separately before — the force one in
`test_tui_force_stop.py`, the Remote Control one in `test_tui_remote_control.py` — and that is
how the Remote Control confirm came to have its row order checked but never the cursor
actually painted on it. A guarantee asserted per flow is a guarantee that covers whichever
flows someone remembered.

So this file parametrizes over `ALL_CONFIRMS`, and its first test is the exhaustiveness half:
a third destructive confirmation registered without an arrangement here **fails** rather than
being silently skipped by a parametrization that only walks what it already knew about.

The guarantees are DEC-007's mitigations, restated as what a confirmation has to do:

* **the row that opens it is offered exactly where the policy allows it** — mitigation 1, at
  the confirm *site* rather than at the modal, because a confirmation nobody can reach and a
  confirmation reachable from a state the policy forbids are both failures the modal itself
  cannot see;
* **the abort rests under the cursor** — a stray enter aborts, and confirming means moving to
  a different row on purpose;
* **no app binding can leave the question unanswered** — this is the one the modal buys and an
  ordinary screen could not;
* **the record is re-read and the policy re-checked after the answer**, and the command that
  is finally issued is the one the row asked for;
* **an answer is only ever chosen, never manufactured** — a confirmation that is cancelled or
  that fails resolves to no, because "the app went away mid-question" must never read as
  consent.

**Every one of those is here because a mutation survived without it.** This file passed on its
first run, which proved nothing; the list above is what a mutation sweep of the surface turned
up afterwards. Where a test says what it can and cannot distinguish, that is measured, not
guessed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from backends import backend_for
from stop_results import a_clean_stop, a_verified_force_stop
from textual.widgets import OptionList
from textual.worker import Worker, WorkerFailed
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.model import _CANCEL
from remote_agents.adapters.tui.screens import ALL_CONFIRMS
from remote_agents.adapters.tui.screens.confirm import (
    ConfirmScreen,
    ForceConfirmModal,
    RemoteControlConfirmModal,
)
from remote_agents.application.commands import ForceStopCommand, RemoteControlCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import (
    FORCE,
    available_actions,
    remote_control_available,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()
#: Interpolated into every status line through `record.display.rendered`, so a modal that
#: forgets to name the session it is about renders a question with this string missing.
_LABEL = "the-one-being-killed"


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1, _LABEL),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Launcher:
    """Records the command objects, and lets the test move the session under the question.

    The commands themselves rather than a name per call: a launcher that recorded
    `"remote-control"` could not tell an Enable from a Disable, and swapping the two
    directions in the surface survived every assertion in an earlier version of this file.
    """

    records: tuple[SessionRecord, ...] = (_record(),)
    issued: list[object] = field(default_factory=list)
    #: When set, every store read blocks on it. The test opens this window deliberately, so
    #: "what is true while a read is in flight" can be asserted without racing the clock.
    gate: asyncio.Event | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        if self.gate is not None:
            await self.gate.wait()
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def force_stop(self, command: ForceStopCommand):
        self.issued.append(command)
        return a_verified_force_stop()

    async def graceful_stop(self, command):
        self.issued.append(command)
        return a_clean_stop()

    async def cleanup(self, command) -> None:
        self.issued.append(command)

    async def set_remote_control(self, command: RemoteControlCommand) -> RemoteControlState:
        self.issued.append(command)
        return RemoteControlState.ACTIVE


def _context(launcher: _Launcher) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
        ),
        profiles=(ProfileChoice("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


@dataclass(frozen=True, slots=True)
class _Arrangement:
    """How to reach one confirmation, when it may be offered, and what answering yes means."""

    #: The session-detail row that opens this confirmation.
    open_key: str
    #: Whether the policy allows this row at all for a given record. Checked against every
    #: `SessionState`, which is DEC-007's rendered-row-parity mitigation applied at the site.
    offered_when: Callable[[SessionRecord], bool]
    #: A state the session can move to while the question is open, in which the policy refuses
    #: the action. Validated below rather than trusted: a state that is merely *filtered out*
    #: of the store would send the test down the record-is-gone path instead, where it would
    #: pass while proving something else entirely.
    refused_state: SessionState
    #: The distinguishing words of the refusal, so the policy path cannot be mistaken for the
    #: record-vanished path — both of which say "no longer available".
    refusal_names: str
    #: What answering yes must put on the launcher, checked as an object rather than a name.
    expects: Callable[[object], bool]


#: One entry per confirm, holding one arrangement per **site** that opens it. Remote Control
#: has two — Enable and Disable — and covering only one of them let a mutation that hard-coded
#: the direction inside the command survive, because the one covered direction was the one
#: hard-coded. The plan's wording is "one case per destructive confirm site"; this is that,
#: rather than one case per modal.
_ARRANGED: dict[type[ConfirmScreen], tuple[_Arrangement, ...]] = {
    # A *muddled-evidence* ORPHANED is a state the stop policy offers no force for -- the
    # fixture leaves orphan_provenance at its default, which is that branch. DEC-020 does
    # offer force for the adopted branch, so this arrangement names which one it means rather
    # than saying "ORPHANED" and meaning half of it. A session that drifts into
    # it while the question is open must not be forced by the answer.
    ForceConfirmModal: (
        _Arrangement(
            open_key=FORCE,
            offered_when=lambda record: (
                FORCE in available_actions(record.state, record.orphan_provenance)
            ),
            refused_state=SessionState.ORPHANED,
            refusal_names="force stop",
            expects=lambda command: (
                isinstance(command, ForceStopCommand) and command.session_id == _SESSION_ID
            ),
        ),
    ),
    # `remote_control_available` requires a RUNNING Claude pane, so PRESERVED is the same
    # shape of refusal for the toggle.
    RemoteControlConfirmModal: tuple(
        _Arrangement(
            open_key=key,
            offered_when=remote_control_available,
            refused_state=SessionState.PRESERVED,
            refusal_names="remote control",
            # The direction the row asked for, not merely "a change happened". Swapping the
            # two rows' states survived a version that checked a name, and hard-coding the
            # direction inside the command survived a version that covered only Enable.
            expects=(
                lambda desired: (
                    lambda command: (
                        isinstance(command, RemoteControlCommand)
                        and command.session_id == _SESSION_ID
                        and command.desired_state is desired
                    )
                )
            )(desired),
        )
        for key, desired in (
            ("remote-control-active", RemoteControlState.ACTIVE),
            ("remote-control-inactive", RemoteControlState.INACTIVE),
        )
    ),
}

#: The flattened (confirm, site) pairs everything driven parametrizes over. The registry above
#: stays keyed by confirm, because that is what the exhaustiveness check compares.
_CASES = tuple(
    pytest.param(confirm, arrangement, id=f"{confirm.__name__}-{arrangement.open_key}")
    for confirm, arrangements in _ARRANGED.items()
    for arrangement in arrangements
)


def test_every_registered_confirm_is_arranged_here() -> None:
    """A confirmation added to the registry without an arrangement fails, rather than skipping.

    The half that makes everything below a sweep rather than a sample. Without it, a third
    destructive confirm would ship with none of these guarantees and every test in this file
    would still be green — verified by registering one and watching ten cases fail.
    """
    assert set(ALL_CONFIRMS) == set(_ARRANGED), (
        "every confirm in ALL_CONFIRMS needs an arrangement in this file, and every "
        "arrangement needs to still be registered"
    )


@pytest.mark.parametrize("confirm", ALL_CONFIRMS, ids=lambda c: c.__name__)
def test_each_arrangements_refused_state_is_genuinely_refused(
    confirm: type[ConfirmScreen],
) -> None:
    """The re-check tests below are only as good as this data, so it is checked, not trusted.

    Two ways the `refused_state` can be wrong, and both leave a green test proving nothing:
    a state the policy actually *allows* makes the refusal test assert a refusal that never
    happens, and a state the store *filters out* — ENDED — sends it down the record-is-gone
    path, where it passes as a verbatim duplicate of the test above it. Substituting ENDED was
    tried; every case still passed.
    """
    for arrangement in _ARRANGED[confirm]:
        record = _record(arrangement.refused_state)
        assert not arrangement.offered_when(record), (
            f"{arrangement.refused_state.value} is a state the policy still allows "
            f"{confirm.__name__} in, so nothing is being refused"
        )
        assert arrangement.refused_state is not SessionState.ENDED, (
            "ENDED is filtered out of the store, so this would test the vanished-record path"
        )


def _keys(app: RemoteAgentsTui) -> list[str | None]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


async def _ask(app: RemoteAgentsTui, pilot, arrangement: _Arrangement) -> asyncio.Task[None]:
    """Open one confirmation the way an owner does — by selecting a row that is rendered.

    Through the widget's own message rather than by calling `choose` directly, and the
    assertion is the point: a surface that stopped offering the row at all would still let a
    direct call reach the modal, so every test here would pass for a confirmation nobody can
    get to. That mutation was tried and survived the version of this helper that called
    `choose`.
    """
    choices = app.screen.query_one("#choices", OptionList)
    assert arrangement.open_key in [option.id for option in choices.options], (
        f"the session detail offers no {arrangement.open_key!r} row, so this confirmation "
        "is unreachable"
    )
    index = choices.get_option_index(arrangement.open_key)
    task = asyncio.create_task(
        app.screen.on_option_list_option_selected(
            OptionList.OptionSelected(choices, choices.get_option_at_index(index), index)
        )
    )
    await pilot.pause()
    return task


async def _confirm(pilot, app: RemoteAgentsTui) -> None:
    """Move onto the confirm row and answer yes, however many rows down it is."""
    choices = app.screen.query_one("#choices", OptionList)
    target = [option.id for option in choices.options].index(app.screen.confirm_key)
    for _ in range(target):
        await pilot.press("down")
    await pilot.press("enter")


# --- mitigation 1: the row exists exactly where the policy allows it -------------------


@pytest.mark.parametrize("state", list(SessionState))
@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_the_row_that_opens_it_is_offered_exactly_where_the_policy_allows(
    confirm, arrangement, state: SessionState
) -> None:
    """DEC-007's first mitigation, at the site rather than at the modal.

    A confirmation is not made safe by its own contents if the surface offers the row that
    opens it in a state the policy forbids — or stops offering it at all. Both were mutated
    in and both survived a version of this file that only ever looked at the modal.
    """
    record = _record(state)
    launcher = _Launcher(records=(record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        offered = arrangement.open_key in _keys(app)

    assert offered is arrangement.offered_when(record), (
        f"{arrangement.open_key!r} is {'offered' if offered else 'missing'} for a "
        f"{state.value} session, which the policy says otherwise about"
    )


# --- mitigation 2: the abort rests under the cursor ------------------------------------


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_the_resting_row_is_the_abort(confirm, arrangement) -> None:
    """A stray enter must abort, whichever confirmation the owner is looking at."""
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)

        assert isinstance(app.screen, confirm), f"{confirm.__name__} was not what opened"
        keys = _keys(app)
        highlighted = app.screen.query_one("#choices", OptionList).highlighted
        assert highlighted is not None, "the owner cannot see which row an enter would activate"
        assert keys[highlighted] == _CANCEL, f"{confirm.__name__} rests on {keys[highlighted]!r}"

        # The bare enter, taken rather than reasoned about.
        await pilot.press("enter")
        await asyncio.wait_for(asking, timeout=5)

    assert launcher.issued == [], f"a single enter on {confirm.__name__} issued {launcher.issued}"


@pytest.mark.parametrize("confirm", ALL_CONFIRMS, ids=lambda c: c.__name__)
def test_the_registered_resting_row_never_mutates(confirm: type[ConfirmScreen]) -> None:
    """The form of the assertion the stage gate sweeps, checked against the rows themselves.

    Asked without driving the surface on purpose: a confirmation nobody has wired a navigation
    path to yet is exactly the one a driven test cannot reach, and it is still one an owner
    will meet as soon as somebody wires it.

    The predicate is compared with the rows rather than simply called, because it is the
    predicate the gate trusts — `initial_focus_is_mutating` hard-coded to `False` makes both
    this test and that sweep vacuous in a single edit, and it survived the version of this
    test that only asked the property about itself.
    """
    modal = confirm()
    assert not modal.initial_focus_is_mutating
    assert modal.rows[modal.resting_index][0] == _CANCEL, (
        f"{confirm.__name__} opens on {modal.rows[modal.resting_index][0]!r}, which is not "
        "the abort — and `initial_focus_is_mutating` disagreed, so the gate's sweep is lying"
    )


class _RestsOnTheMutation(ConfirmScreen):
    """A confirm that opens on its own confirm row — what the gate's sweep must catch.

    Not registered. It exists so the predicate can be asked a question whose answer is `True`,
    which is the only way to tell a working detector from one that says no to everything.
    """

    position = "RESTS_ON_THE_MUTATION"
    question = "?"
    confirm_key = "do-it"
    confirm_label = "Do it"
    resting_index = 1


def test_the_predicate_the_gate_sweeps_can_actually_detect_a_mutating_resting_row() -> None:
    """`initial_focus_is_mutating` hard-coded to `False` passes every other test in this file.

    Verified by mutating it: the driven tests still saw an abort-first modal and the
    class-level assertion still saw abort-first rows, so the gate's whole sweep over
    `ALL_CONFIRMS` became vacuous in one edit and nothing said so. A detector is only evidence
    if it has been shown to fire.
    """
    assert _RestsOnTheMutation().initial_focus_is_mutating, (
        "the predicate the stage gate sweeps `ALL_CONFIRMS` with cannot detect a confirm "
        "resting on its own mutating row, so that sweep proves nothing"
    )


class _ThreeWay(ConfirmScreen):
    """A confirm offering a third, neutral row — the shape `rows` says a subclass may take.

    Not registered, and deliberately so: this is here to pin what `ConfirmScreen` promises for
    a confirm that does not yet exist. With two rows, `dismiss(id == confirm_key)` and
    `dismiss(id != _CANCEL)` are the same function, and the second one answers **yes** to this
    screen's middle row.
    """

    position = "THREE_WAY"
    question = "which?"
    confirm_key = "do-it"
    confirm_label = "Do it"

    @property
    def rows(self) -> tuple[tuple[str, str], ...]:
        return ((_CANCEL, "Cancel"), ("something-else", "Something else"), ("do-it", "Do it"))


async def test_only_the_confirm_row_answers_yes() -> None:
    """Every row that is not *the* confirm row is a no, including one nobody anticipated."""
    app = RemoteAgentsTui(_context(_Launcher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        asking = asyncio.create_task(app.ask_to_confirm(_ThreeWay()))
        await pilot.pause()
        await pilot.press("down")  # onto "Something else", the unanticipated middle row
        await pilot.press("enter")
        assert await asyncio.wait_for(asking, timeout=5) is False

        answering = asyncio.create_task(app.ask_to_confirm(_ThreeWay()))
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")  # onto "Do it"
        await pilot.press("enter")
        assert await asyncio.wait_for(answering, timeout=5) is True


# --- mitigation 3: no app binding can leave the question unanswered ---------------------

#: Derived from the app's own bindings rather than listed by hand. A hand-written list covers
#: the bindings someone remembered on the day: it had four entries, and `ctrl+q` — which would
#: take the app down with the question open — was not among the ones being *checked*, only
#: among the ones being described in prose.
_APP_BINDINGS = tuple(
    binding.key for binding in RemoteAgentsTui.BINDINGS if binding.key != "escape"
)


@pytest.mark.parametrize("binding", _APP_BINDINGS)
@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_an_app_binding_cannot_dismiss_the_question_unanswered(
    confirm, arrangement, binding: str
) -> None:
    """The guarantee the modal buys, and the reason this stage exists.

    **Asserted on the observable outcome first.** An earlier version asserted only that the
    key was absent from `active_bindings`, and that read is built from the *same* truncated
    chain the modal produces — so it agrees with the modal by construction and cannot see a
    binding that fires by another route. What matters is that the question is still open,
    nothing was issued, and the app is still running.

    `escape` is excluded because it is the one app binding whose key the modal deliberately
    rebinds to answer the question. Everything else the app binds must leave it alone.
    """
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)
        step = position(app)

        await pilot.press(binding)
        await pilot.pause()

        assert app.is_running, f"{binding} took the app down with a confirmation open"
        assert not asking.done(), (
            f"{binding} answered the {confirm.__name__} question on the owner's behalf"
        )
        assert position(app) == step, f"{binding} navigated away from {confirm.__name__}"
        assert isinstance(app.screen, confirm)

        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)

    assert launcher.issued == []


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_the_binding_chain_itself_is_truncated_at_the_modal(confirm, arrangement) -> None:
    """The mechanism, separately from the outcome, because the outcome has two causes.

    The keypress test above passes without any modal at all: the busy guard is held across the
    whole question and the flow-switching actions already refuse while busy. Verified by
    mutating `ConfirmScreen` back to an ordinary `Screen` — that test stayed green and this one
    does not. Both are kept because they are two mechanisms, and the guard is a flag a later
    refactor could release while the question is open.

    **What this does not cover, stated because measuring it is the only way to know:**
    `_modal_binding_chain` is consulted for ordinary bindings only. `App._check_bindings`
    takes the *untruncated* chain for `priority=True` bindings, so Textual's own command
    palette (`ctrl+p`) still opens over a confirmation — confirmed by driving it. That is not
    an unanswered-question escape (the palette pushes above the modal, the question stays
    open, escape returns to it), and if the app is quit from the palette the answer resolves
    to no rather than to consent, which the cancellation test below is what pins.
    """
    app = RemoteAgentsTui(_context(_Launcher()))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)

        active = app.screen.active_bindings
        leaked = [key for key in _APP_BINDINGS if key in active]
        assert not leaked, (
            f"{leaked} are still in {confirm.__name__}'s binding chain, so only the busy "
            "guard is stopping them from acting on an unanswered question"
        )

        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)


# --- an answer is chosen, never manufactured -------------------------------------------


class _NeverAnswers(ConfirmScreen):
    """A confirmation that is never answered, so the test can decide how it ends."""

    position = "NEVER_ANSWERS"
    question = "waiting"
    confirm_key = "yes"
    confirm_label = "Yes"


async def test_a_cancelled_confirmation_is_not_consent() -> None:
    """The app going away mid-question resolves to no.

    `_ask`'s worker is owned by the app and cancelled when it shuts down, and this is the one
    place in the whole machinery where a `bool` is produced by something other than the owner
    choosing it. Turning `ask_to_confirm`'s `except WorkerCancelled: return False` into
    `return True` was mutated in and survived everything else in this file — a cancelled
    confirmation would then have walked straight into a force stop.
    """
    app = RemoteAgentsTui(_context(_Launcher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        asking = asyncio.create_task(app.ask_to_confirm(_NeverAnswers()))
        await pilot.pause()

        app.workers.cancel_all()

        assert await asyncio.wait_for(asking, timeout=5) is False


async def test_a_failed_confirmation_is_not_consent() -> None:
    """A worker that raises surfaces the error; it does not answer the question.

    Driven by handing `ask_to_confirm` a worker that fails, rather than by breaking a real
    modal: a modal that raises while mounting takes the app down, and the answer then resolves
    through the cancellation path above — so the `WorkerFailed` branch is not reachable that
    way and would go untested by any end-to-end arrangement. This tests the translation
    directly, which is where the mutation lives: `raise failure.error` → `return True`.
    """
    app = RemoteAgentsTui(_context(_Launcher()))

    class _Failed:
        async def wait(self):
            raise WorkerFailed(RuntimeError("the dialog could not be built"))

    app._ask = lambda _screen: _Failed()  # type: ignore[assignment,method-assign]

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        with pytest.raises(RuntimeError, match="could not be built"):
            await app.ask_to_confirm(_NeverAnswers())


def test_the_failure_stand_in_matches_the_real_worker() -> None:
    """The test above fakes a `Worker`, so this is what keeps the fake honest.

    A stand-in that drifted from the real type would let the test pass against a method that
    no longer exists. `Worker.wait` is the whole of what `ask_to_confirm` uses.
    """
    assert callable(Worker.wait)


# --- mitigation 4: re-read, re-check, and issue what the row asked for -------------------


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_a_session_that_vanished_while_the_question_was_open_is_not_acted_on(
    confirm, arrangement
) -> None:
    """DEC-007's fourth mitigation: the record is re-read at issue time, not trusted."""
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)

        launcher.records = ()

        await _confirm(pilot, app)
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        status = _status(app)

    assert launcher.issued == [], f"{confirm.__name__} acted on a session that had gone"
    assert "no longer available" in status.casefold()
    # The vanished-record message is the *bare* one. Asserted here so this test and the
    # policy test below cannot both be satisfied by the same string, which is what let a
    # mutation that replaced the policy refusal with this exact message survive both.
    assert arrangement.refusal_names not in status.casefold(), (
        "the vanished-record path named the action, so it is indistinguishable from the "
        "policy-refusal path and neither test pins which one ran"
    )


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_a_policy_that_stopped_allowing_it_refuses_the_answer(confirm, arrangement) -> None:
    """The other half of the same mitigation: the *policy* is re-checked, not just the record.

    A session that is still there but has moved to a state where the action is no longer
    offered must not have that action issued because the owner answered a question drawn from
    an earlier state. The service would refuse it anyway; what this pins is that the owner
    reads an explanation naming the action, rather than the bare "that session is gone" — a
    distinction that is asserted rather than assumed, because a mutation collapsing the two
    messages into one survived when it was not.

    The two now land in *different sinks* — the vanished record in the status line, the policy
    refusal in a `warning` toast — which is the status split doing the same job this pair of
    tests was written to do by hand. Read from the toast rather than from `#status`, or this
    passes on a surface that says nothing at all.
    """
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)

        launcher.records = (_record(arrangement.refused_state),)

        await _confirm(pilot, app)
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        refusals = [message.casefold() for message in announcements(app, severity="warning")]

    assert launcher.issued == [], (
        f"{confirm.__name__} issued a command for a session in "
        f"{arrangement.refused_state.value}, which the policy does not offer it for"
    )
    assert any("no longer available" in message for message in refusals), refusals
    assert any(arrangement.refusal_names in message for message in refusals), (
        "the refusal did not name the action, so the owner cannot tell which of the two "
        "reasons applied and this test cannot either"
    )


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_answering_yes_issues_exactly_the_command_the_row_asked_for(
    confirm, arrangement
) -> None:
    """The positive control, on the command object rather than on a count.

    Without the payload check every assertion above would hold for a surface that confirmed
    the right question and then issued the opposite direction — mutated in by swapping the
    two Remote Control rows' states, which survived a version that counted commands.
    """
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)
        await _confirm(pilot, app)
        await asyncio.wait_for(asking, timeout=5)

    assert len(launcher.issued) == 1, f"answering yes issued {launcher.issued}"
    assert arrangement.expects(launcher.issued[0]), (
        f"{confirm.__name__} issued {launcher.issued[0]!r}, which is not what its row asked for"
    )


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_the_guard_is_still_held_while_the_abort_refreshes(confirm, arrangement) -> None:
    """Declining must not leave a window where the pre-modal rows are live and unguarded.

    The abort re-reads the detail, and that read awaits. Until the refresh lands, the rows on
    screen are the ones from before the question was asked — with the cursor still on the row
    that opened it. If the busy guard is released before the refresh rather than after, a
    second enter in that gap dispatches the *old* row again and opens a second confirmation on
    top of the first one's redraw. Nothing could be killed by it, since each stacked question
    still needs its own yes; it is the await-then-render window `showing` and
    `holding_the_guard` close everywhere else in this surface.

    **Asserted as the property, not as the race.** The first version of this test declined and
    then pressed enter, and it passed against the defect — `pilot.press` pumps, so the refresh
    had already finished by the time the second key arrived. Reproducing the interleaving by
    timing would make the test a coin flip; the store read is gated instead, so "the guard is
    held while the read is in flight" is asked directly and answers the same way every run.
    """
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)

        # From here on, any store read blocks — so the abort's refresh is caught mid-flight.
        launcher.gate = asyncio.Event()
        await pilot.press("escape")
        await pilot.pause()

        assert not asking.done(), "the abort returned without refreshing the detail at all"
        assert app.busy, (
            "the busy guard was released before the abort's re-read, so the detail is "
            "showing its pre-modal rows with nothing refusing a keypress on them"
        )

        launcher.gate.set()
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        assert not app.busy, "the guard was never released"
        assert position(app) == "SESSION_DETAIL"

    assert launcher.issued == []


@pytest.mark.parametrize("bursts", [2, 3, 4, 8])
@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_a_burst_of_answers_answers_once_and_pops_once(
    confirm, arrangement, bursts: int
) -> None:
    """Several key events in one pump turn must answer the question once, not once per key.

    `Screen.dismiss` calls the result callback (if one is left) and then `app.pop_screen()`
    unconditionally — and `pop_screen` pops the *top of the stack*, which after the first
    dismiss is no longer this screen. So a second answer popped the session detail, a third
    took the sessions list with it, and a fourth raised `ScreenStackError` out of a message
    handler and killed the app.

    **Posted rather than pressed, and that is the whole reason this defect survived the
    suite.** `Pilot.press` awaits idle between keys, so it can never produce the burst; key
    autorepeat, a double-tap, and buffered stdin over a laggy link all can, because the driver
    parses one read into several key events posted back to back. A test that presses is
    testing a shape the terminal does not always send.
    """
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_sessions()
        await pilot.pause()
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        depth = len(app.screen_stack)
        asking = await _ask(app, pilot, arrangement)
        modal = app.screen
        choices = modal.query_one("#choices", OptionList)
        chosen = choices.get_option_at_index(modal.resting_index)

        # No pump turn between them, which is what a single terminal read delivers.
        for _ in range(bursts):
            modal.post_message(OptionList.OptionSelected(choices, chosen, modal.resting_index))
        await pilot.pause()
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()

        assert app.is_running, f"{bursts} answers in one turn took the app down"
        assert len(app.screen_stack) == depth, (
            f"{bursts} answers popped {depth - len(app.screen_stack)} screens; "
            "one question is one pop"
        )
        assert position(app) == "SESSION_DETAIL", (
            f"the owner was left on {position(app)} rather than the position they asked from"
        )

    assert launcher.issued == [], "the resting row is the abort, so a burst must issue nothing"


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_a_store_failure_reported_under_a_modal_does_not_crash(confirm, arrangement) -> None:
    """BL-021, which the Stage 2 gate recorded and predicted this stage would make reachable.

    `RemoteAgentsTui.body` was an unchecked cast to `ChoiceScreen`, safe only while nothing
    else could be on top. A confirmation is not a `ChoiceScreen` and has no `show_choices`, so
    a store failure reported while one is open called a method that does not exist — from the
    path that exists to report trouble *without* losing the app.
    """
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)
        assert isinstance(app.screen, ConfirmScreen)

        # The assertion is that this returns at all.
        app.report_store_failure(RuntimeError("the store went away"))
        await pilot.pause()

        assert app.is_running, "reporting a store failure under a modal took the app down"
        assert isinstance(app.screen, confirm), "the report redrew over the open question"

        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)

    assert launcher.issued == []


@pytest.mark.parametrize("confirm,arrangement", _CASES)
async def test_the_question_names_the_session_it_is_about(confirm, arrangement) -> None:
    """An owner confirming a kill must be able to see which session they are killing.

    Blanking the rendered question, and dropping the session's name from it, were both
    mutated in and both survived — the whole "a second surface may destroy things" argument
    rests on the owner being told what they are agreeing to.
    """
    app = RemoteAgentsTui(_context(_Launcher()))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
        asking = await _ask(app, pilot, arrangement)
        question = _status(app)
        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)

    assert _LABEL in question, (
        f"{confirm.__name__} asks a question that does not name the session: {question!r}"
    )
