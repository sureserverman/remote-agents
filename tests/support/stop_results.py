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
