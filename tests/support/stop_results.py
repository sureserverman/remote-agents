"""What a test double standing in for `SessionService.graceful_stop` must hand back.

`SessionService.graceful_stop` is typed `-> TerminalObservation` and never returns `None`.
Both surfaces discarded that value until BL-008, so every double in the suite could return
nothing and no test noticed — and when the surfaces started reading it, the doubles that still
returned `None` did not fail. They kept passing while quietly exercising the *exception*
branch, because `stop_failure(None)` raises `AttributeError` straight into the caller's
`except Exception`. A test called "a repeated keypress issues exactly one stop" was, for a
while, only ever testing a stop that crashed.

So the helpers are shared rather than written per file. There are a dozen of these doubles
across the TUI, Telegram, contract and e2e suites; the point of one module is that the next
change to what a stop reports has one place to update instead of a dozen chances to miss one.

`session_id` is optional because most callers do not care which session the observation names
— they are asserting on what the *surface* said, and the id never reaches the wording.
"""

from __future__ import annotations

from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import TerminalObservation


def a_clean_stop(session_id: SessionId | None = None) -> TerminalObservation:
    """The profile's own exit sequence ran and the pane exited — `preserved` is what says so.

    The right default for any double that does not care about the outcome, because it is the
    outcome those tests were written against back when the value was thrown away.
    """
    return TerminalObservation(session_id or SessionId.new(), live=False, preserved=True)


def a_stop_that_did_not_take(
    detail: str, session_id: SessionId | None = None
) -> TerminalObservation:
    """A graceful stop that left the session running, for the reason `detail` names.

    `live=True` and `preserved=False` together are what both non-preserving causes report:
    nothing was removed, and the agent is still there. See
    `application.session_actions.stop_failure` for what each `detail` means to the owner.
    """
    return TerminalObservation(
        session_id or SessionId.new(), live=True, preserved=False, detail=detail
    )


def a_verified_force_stop(session_id: SessionId | None = None) -> TerminalObservation:
    """The managed pane was found and killed, which is what force stop does when it works.

    `preserved` is false and stays false — force removes the pane rather than keeping it, so
    unlike a graceful stop there is no outcome here in which it is true. That is exactly why
    `stop_failure` cannot read a force: it keys on `preserved`, and every force would look like
    a failure to it. `force_stop_failure` reads the detail instead.

    The right default for any double standing in for `SessionService.force_stop`, because it is
    the outcome those tests were written against while the value was being thrown away.
    """
    return TerminalObservation(session_id or SessionId.new(), live=False, preserved=False)


def a_force_stop_that_found_nothing(session_id: SessionId | None = None) -> TerminalObservation:
    """Force stop ran and no managed pane matched, so nothing was killed (BL-026, DEC-017).

    `TmuxRuntime.force_stop` reports `ownership_lost` here, and `SessionService.force_stop`
    records `VERIFIED_FORCE_STOP` anyway — deliberately, per DEC-017, so the record still
    reaches ENDED and the row still clears rather than stranding. What changes is only what the
    surfaces are entitled to *say* about it.

    Not the ordinary "the agent already exited" case, which this is easy to mistake for: a
    cleanly exited agent leaves a PRESERVED pane, still in the managed inventory, so force
    finds it and kills it normally. This fires only when the pane is absent from the inventory
    entirely — destroyed outside the app, or ownership metadata drifted.
    """
    return TerminalObservation(
        session_id or SessionId.new(), live=False, preserved=False, detail="ownership_lost"
    )


def a_reader_for(record):
    """A `read_record` callable for `application.stops.execute_stop`, over a fixed record.

    `execute_stop` performs the re-read itself rather than trusting one handed in — that is
    the whole of DEC-007's fourth mitigation and DEC-008's 2026-08-08 correction — so every
    caller supplies the store read rather than the record. In a test the read is usually a
    constant, and this is that constant wearing the right shape.

    Shared for the reason the observation helpers above are: the dispatch's callers live in
    five test files across four suites, and a signature change should have one place to
    update rather than a dozen chances to miss one.
    """

    async def read():
        return record

    return read
