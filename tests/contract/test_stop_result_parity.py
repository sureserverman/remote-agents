"""Both surfaces say the same thing about a stop that did not take effect.

The sibling of `test_session_actions_parity.py`, and written to the same rule for the same
reason: against what each surface **rendered**, never against the `TerminalObservation` or the
`StopFailure` object. A surface that reads the shared vocabulary and then rewords it, drops
half of it, or renders it into a region nobody sees would satisfy every assertion an
object-level test could make. DEC-007 is about what the operator is told, so this asks the
surfaces what they told them.

What BL-008 recorded: `SessionService.graceful_stop` has always returned an observation that
tells a clean exit from a failure, and **both surfaces discarded it**. The local one said
nothing at all and left the session on screen still running; the bot inferred "It did not exit
in time" from the session still being listed, which is right for one of the two causes and
confidently wrong for the other.

**What changed under this file, and what it still detects (DEC-019).** Both surfaces now
dispatch through `application/stops.py`, so the `StopFailure` each one is handed is
necessarily the same object — the *vocabulary* half of the parity below is structural now and
can no longer fail. What remains genuinely testable, and is the reason this file drives two
real UIs rather than comparing two function calls, is what each surface **does with it**: the
bot escapes it into HTML and lands it on a notice, the local surface splits it across a status
line and a toast. A surface that reworded the summary, dropped the remedy, or rendered either
into a region nobody reads would still satisfy every object-level assertion, and this file
would still catch it. That is the claim; the shared dispatch narrowed it rather than voiding
it.

So there are three claims about a **graceful** stop here, and they fail separately:

* each surface **names the cause** — the fix on that surface;
* the two surfaces **agree** — DEC-007, and the thing a single shared vocabulary buys;
* the two causes **do not read alike** on either surface — the half BL-008 would otherwise
  close without answering, since one message for both causes satisfies the first two claims.

**And a fourth, about force.** The force-stop tests below are not instances of the three:
`test_a_force_that_killed_the_pane_still_reads_as_a_stop_that_worked` asserts the opposite
direction — that a force which *did* work is never reported as a failure — which is the trap
DEC-017 sets, since `preserved` is false on every force including the successful one. Counted
separately because "three claims" read as the file's whole inventory and was not.

The detail values are spelled as literals rather than imported as constants, deliberately.
They are the strings `adapters/tmux/runtime.py` puts on the wire, and a contract test that
imported the constants would keep passing if a rename changed what the adapter actually emits.
`test_the_constants_still_name_the_wire_values` pins the two together in one place instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import unescape

import pytest
from backends import SessionUseCaseDouble, backend_for
from surfaces import surface_pairs
from textual.widgets import OptionList

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import GRACEFUL_TIMEOUT, UNKNOWN_SESSION
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation

#: The two causes a graceful stop can report without preserving the pane. A configuration
#: problem and an agent-behaviour problem — the owner's next step differs completely, which is
#: why "they must not read alike" is a requirement rather than a nicety.
_CAUSES = ("unknown_session", "graceful_timeout")

_PROJECT_ID = ProjectId("a" * 24)


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        _PROJECT_ID,
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


class _Launcher(SessionUseCaseDouble):
    """A graceful stop that reports `detail` and leaves the session exactly where it was.

    Leaving it listed is not incidental — it is the evidence both surfaces used to have, and
    all they had. Both causes produce this identical aftermath, which is why neither surface
    could tell them apart by looking at the record.
    """

    def __init__(self, record: SessionRecord, detail: str) -> None:
        self.record = record
        self.detail = detail
        self.issued: list[object] = []

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None

    async def inspect(self, _query):
        return None

    async def graceful_stop(self, command):
        self.issued.append(command)
        return TerminalObservation(
            self.record.session_id, live=True, preserved=False, detail=self.detail
        )


async def _telegram_said(record: SessionRecord, detail: str) -> str:
    """Everything the bot's reply put in front of the owner, as one string.

    HTML-unescaped, because this asks what the *operator reads* and Telegram renders the
    entities back to characters — the remedy contains an apostrophe, so a raw comparison
    against the shared vocabulary fails on `&#x27;` while the surfaces are in perfect
    agreement. Unescaping here rather than weakening the assertion keeps the comparison exact.
    """
    launcher = _Launcher(record, detail)
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(str(_PROJECT_ID), "opaque-editor", "tests", "Registered"),),
            sessions=launcher,
        ),
    )
    token = boundary.stops.offer(
        record.session_id, record.profile_id, record.state, None, "graceful", 7, 11
    )
    assert token is not None, "the bot offered no graceful stop, so nothing was exercised"
    # The token is minted unbound, exactly as a real render mints it; delivering the screen
    # is what makes it pressable.
    boundary.callbacks.bind_pending(11, 100)
    reply = await boundary._stop_reply("graceful", token, 100)
    assert launcher.issued, "the bot never issued the stop, so this asserts nothing about it"
    return unescape(str(reply["text"]))


async def _tui_said(record: SessionRecord, detail: str) -> str:
    """Everything the local surface put in front of the owner, as one string.

    Both sinks, joined. The status split gave this surface a one-line status and a
    notification, and reading only one of them would let a regression that moved the whole
    message into the other pass — which is the same class of miss as reading the policy
    instead of the rendered rows.
    """
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    launcher = _Launcher(record, detail)
    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=launcher,  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        rows = app.screen.query_one("#choices", OptionList)
        assert "graceful" in [option.id for option in rows.options], (
            "the detail offered no graceful stop, so nothing was exercised"
        )
        await app.screen.choose("graceful")
        await pilot.pause()
        status = str(app.screen.query_one("#status").content)
        toasts = " ".join(notification.message for notification in app._notifications)
    assert launcher.issued, "the surface never issued the stop, so this asserts nothing about it"
    return f"{status}\n{toasts}"


SURFACES = surface_pairs(telegram=_telegram_said, tui=_tui_said)


@pytest.mark.parametrize("detail", _CAUSES)
def test_the_constants_still_name_the_wire_values(detail: str) -> None:
    """The literals above are the adapter's, and this is where they meet the constants.

    Spelling them out everywhere else is what makes this file a contract rather than a
    restatement of an import; pinning them here once is what stops the literals silently
    drifting away from the names the surfaces actually branch on.
    """
    assert detail in {UNKNOWN_SESSION, GRACEFUL_TIMEOUT}


@pytest.mark.parametrize("surface_name,said", SURFACES)
@pytest.mark.parametrize("detail", _CAUSES)
async def test_each_surface_says_a_stop_did_not_take_effect(
    surface_name: str, said, detail: str
) -> None:
    """The first claim: something is said at all. This is the literal text of BL-008."""
    rendered = await said(_record(), detail)
    assert rendered.strip(), f"{surface_name} rendered nothing for {detail}"
    assert "still running" in rendered.casefold() or "did not take effect" in rendered.casefold(), (
        f"{surface_name} reported {detail} as {rendered!r}, which does not say the stop failed"
    )


@pytest.mark.parametrize("detail", _CAUSES)
async def test_both_surfaces_name_the_same_cause(detail: str) -> None:
    """DEC-007: an operator moving between the surfaces must not be told two different things.

    Compared through the shared vocabulary's own sentence rather than a marker word each
    surface could satisfy separately — that sentence is authored once in
    `application.session_actions`, so this fails the moment either surface starts wording the
    cause for itself.
    """
    from remote_agents.application.session_actions import stop_failure

    failure = stop_failure(
        TerminalObservation(SessionId.new(), live=True, preserved=False, detail=detail)
    )
    assert failure is not None
    said = {name: await render(_record(), detail) for name, render in SURFACES}

    for name, rendered in said.items():
        assert failure.summary in rendered, (
            f"{name} did not use the shared vocabulary for {detail}; it said {rendered!r}"
        )
        # The remedy as well as the summary, because they are read for different things and
        # only one of them is actionable. Asserting the summary alone — which this test did
        # until a gate evaluator measured it — passes a surface that names the cause and
        # silently drops what to do about it, which is most of what the vocabulary is for.
        assert failure.remedy in rendered, (
            f"{name} named the cause for {detail} but not the remedy; it said {rendered!r}"
        )


class _ForceLauncher(SessionUseCaseDouble):
    """A force stop that reports `detail` and leaves the session gone from the list.

    Gone, on both outcomes, and that is the point of BL-026 rather than a convenience.
    `SessionService.force_stop` records `VERIFIED_FORCE_STOP` whether or not `kill-session`
    ran, so the record reaches ENDED and drops out of the listing either way (DEC-017, chosen
    because a row the owner cannot clear is worse than an over-confident message). The
    aftermath is therefore *identical* for a kill that happened and one that did not — exactly
    the shape BL-008 named for graceful, in the one action that kills. The observation is the
    only thing that can tell them apart, and both surfaces used to discard it.
    """

    def __init__(self, record: SessionRecord, detail: str) -> None:
        self.record = record
        self.detail = detail
        self.issued: list[object] = []
        self.stopped = False

    async def refresh_readiness(self):
        return () if self.stopped else (self.record,)

    async def list_sessions(self):
        return () if self.stopped else (self.record,)

    async def copy_attach(self, _session_id):
        return None

    async def inspect(self, _query):
        return None

    async def force_stop(self, command):
        self.issued.append(command)
        self.stopped = True
        return TerminalObservation(
            self.record.session_id, live=False, preserved=False, detail=self.detail
        )


async def _telegram_said_force(record: SessionRecord, detail: str) -> str:
    """Everything the bot's reply put in front of the owner after a confirmed force."""
    from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE

    launcher = _ForceLauncher(record, detail)
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(str(_PROJECT_ID), "opaque-editor", "tests", "Registered"),),
            sessions=launcher,
        ),
    )
    # The *confirmed* token, because an unconfirmed force is refused at `claim` by design —
    # the second press is what makes it runnable, and this drives the press that runs.
    token = boundary.stops.offer_confirmed_force(
        record.session_id, record.profile_id, record.state, record.orphan_provenance, 7, 11
    )
    assert token is not None, "the bot offered no force stop, so nothing was exercised"
    boundary.callbacks.bind_pending(11, 100)
    reply = await boundary._stop_reply(CONFIRMED_FORCE, token, 100)
    assert launcher.issued, "the bot never issued the force, so this asserts nothing about it"
    return unescape(str(reply["text"]))


async def _tui_said_force(record: SessionRecord, detail: str) -> str:
    """Everything the local surface put in front of the owner after a confirmed force.

    The confirmation is a modal awaited through `ask_to_confirm`, so `choose` does not return
    until it is answered — the same arrangement as a real keypress, whose handler is likewise
    suspended for as long as the owner takes to decide. It is answered here by pressing the
    confirming row rather than by reaching past the modal, so what is exercised is the path an
    owner actually takes.
    """
    import asyncio

    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    launcher = _ForceLauncher(record, detail)
    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=launcher,  # type: ignore[arg-type]
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=tuple,
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        rows = app.screen.query_one("#choices", OptionList)
        assert "force" in [option.id for option in rows.options], (
            "the detail offered no force stop, so nothing was exercised"
        )
        asking = asyncio.create_task(app.screen.choose("force"))
        await pilot.pause()
        # The modal rests on the abort by design (DEC-007), so confirming is two deliberate
        # acts: move off Cancel, then press. Driven with keys rather than by calling into the
        # modal, because the modal is a `ModalScreen[bool]` with no `choose` — and because
        # this is the sequence an owner actually performs.
        await pilot.press("down")
        await pilot.press("enter")
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        status = str(app.screen.query_one("#status").content)
        toasts = " ".join(notification.message for notification in app._notifications)
    assert launcher.issued, "the surface never issued the force, so this asserts nothing about it"
    return f"{status}\n{toasts}"


FORCE_SURFACES = surface_pairs(telegram=_telegram_said_force, tui=_tui_said_force)


@pytest.mark.parametrize("surface_name,said", FORCE_SURFACES)
async def test_neither_surface_claims_a_kill_it_did_not_make(surface_name: str, said) -> None:
    """BL-026, as the operator meets it. The third cause, and the only one on the kill path.

    `ownership_lost` means `TmuxRuntime.force_stop` matched no managed pane and killed nothing.
    Both surfaces reported the kill anyway — a claim about an observation that says the
    opposite, and in the drifted-metadata case an agent may still be running behind it
    (DEC-017's accepted cost 2).

    **Asserted as what the surface must *say*, not only as a phrase it must avoid.** The
    negative alone was measured against the pre-fix code and caught only the bot, because the
    local surface never used the words "Force stopped" — it said "the session has ended", which
    is true and still omits everything the owner needs. A forbidden-phrase list only ever
    catches the wording somebody already thought of.
    """
    from remote_agents.application.session_actions import force_stop_failure

    observed = force_stop_failure(
        TerminalObservation(SessionId.new(), live=False, preserved=False, detail="ownership_lost")
    )
    assert observed is not None
    rendered = await said(_record(), "ownership_lost")

    assert observed.summary in rendered, (
        f"{surface_name} ended the session without saying no pane was found: {rendered!r}"
    )
    assert "force stopped" not in rendered.casefold(), (
        f"{surface_name} still claims a kill it did not make: {rendered!r}"
    )


async def test_both_surfaces_name_the_same_cause_for_a_force_that_found_nothing() -> None:
    """DEC-007, extended to the kill path: one vocabulary, authored once.

    Compared through the shared sentence rather than a marker word, for the reason the graceful
    sibling gives: a marker each surface could satisfy separately is not agreement.
    """
    from remote_agents.application.session_actions import force_stop_failure

    failure = force_stop_failure(
        TerminalObservation(SessionId.new(), live=False, preserved=False, detail="ownership_lost")
    )
    assert failure is not None
    said = {name: await render(_record(), "ownership_lost") for name, render in FORCE_SURFACES}

    for name, rendered in said.items():
        assert failure.summary in rendered, (
            f"{name} did not use the shared vocabulary for ownership_lost; it said {rendered!r}"
        )
        assert failure.remedy in rendered, (
            f"{name} named the cause but not the remedy; it said {rendered!r}"
        )


@pytest.mark.parametrize("surface_name,said", FORCE_SURFACES)
async def test_a_force_that_killed_the_pane_still_reads_as_a_stop_that_worked(
    surface_name: str, said
) -> None:
    """The other half, and the one a fix for BL-026 could break without noticing.

    Routing force through `stop_failure` — the obvious reuse — would report *every* force as a
    failure, because that function keys on `preserved` and force never preserves. This is the
    assertion that catches it: a force that found its pane and killed it must still read as a
    stop that worked, and must not carry the ownership_lost wording.
    """
    rendered = await said(_record(), "")

    assert "This host had no pane left to stop." not in rendered, (
        f"{surface_name} reported a completed force stop as a failure: {rendered!r}"
    )
    assert "ended" in rendered.casefold() or "force stopped" in rendered.casefold(), (
        f"{surface_name} did not report a completed force stop as one: {rendered!r}"
    )


@pytest.mark.parametrize("surface_name,said", FORCE_SURFACES)
async def test_the_two_force_outcomes_do_not_read_alike_on_either_surface(
    surface_name: str, said
) -> None:
    """The claim the other three cannot make, and the whole of BL-026.

    The record ends and the row clears on both outcomes, so the *aftermath* is identical — one
    message for both satisfies every other assertion here perfectly, and was what both surfaces
    shipped. Compared as whole rendered messages so wording that has converged fails too.
    """
    rendered = {detail: await said(_record(), detail) for detail in ("", "ownership_lost")}
    assert rendered[""] != rendered["ownership_lost"], (
        f"{surface_name} says the same thing whether or not a pane was found: {rendered['']!r}"
    )


@pytest.mark.parametrize("surface_name,said", SURFACES)
async def test_the_two_causes_do_not_read_alike_on_either_surface(surface_name: str, said) -> None:
    """The claim the other two cannot make, and the one BL-008 would otherwise leave open.

    A single message rendered for both causes satisfies "something is said" and "both surfaces
    agree" perfectly. It is also the exact defect this closes on the Telegram side, which used
    to assert "It did not exit in time" for `unknown_session` too — where no exit sequence was
    ever sent, and an owner who believed it would wait for an agent nobody had asked to stop.

    Compared as whole rendered messages rather than by hunting for marker strings, so wording
    that has *converged* fails here and not only wording that was never written.
    """
    rendered = {detail: await said(_record(), detail) for detail in _CAUSES}
    assert rendered["unknown_session"] != rendered["graceful_timeout"], (
        f"{surface_name} says the same thing for both causes: {rendered['unknown_session']!r}"
    )
