"""The local surface never offers "Trust this project", and that is the intended behaviour.

The owner is already looking at the dialog. DEC-040 has the console exchange its left pane
with the agent's, so the Claude Code trust prompt is on screen on this surface and is
answered by typing into it. A row re-asking it would be duplication — and worse than
redundant, since it would put a second, differently-worded route to a security-relevant
answer beside the real one. On Telegram there is no pane, so the bot's twin path is not
duplication and stays. That asymmetry is DEC-047, which supersedes DEC-016's "both
surfaces" clause.

**This file used to say the opposite, and the story is the point.** It was written as
`test_trust_row_bl005.py`, to hold a *defect* in place: DEC-016's closing sentence was read
as requiring parity, so a surface not offering the row looked broken. Its own docstring
instructed the next reader to **delete this file** once somebody "fixed" BL-005. The owner's
reasoning — that a pane you are looking at needs no second question — had simply never been
written down, so four artifacts argued the other way for a day: DEC-016's clause, the
backlog entry, `_observed_trust`'s docstring, and this file.

So the assertions below are unchanged and their meaning is inverted. **Do not delete this
file.** It is not standing in the way of a repair; it is what stops one. A change that makes
these fail is adding a second answer path to a security question, and it needs DEC-047
superseded first.

The three assertions still earn their place together: the row is absent for a `claude`
session whose pane really is showing the dialog; the backend is never asked, so nothing is
reaching for a trust state behind the scenes; and the policy *would* offer the row for that
record, which is what stops the first two passing for an unrelated reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList

from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.trust import TrustState

TRUST_ROW = "Trust this project"


def _awaiting_record() -> SessionRecord:
    """A `claude` session, RUNNING — the exact record `trust_available` says may be asked.

    RUNNING rather than a blocked-looking state on purpose: `_observed_trust` documents that
    a trust-blocked `claude-remote` launch lands RUNNING because its readiness marker is
    observed before the dialog renders. State is not evidence about the dialog; the pane is.
    """
    return SessionRecord(
        SessionId(UUID(int=1)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 22, tzinfo=UTC),
    )


class _PaneIsAwaitingTrust(SessionUseCaseDouble):
    """A session use case that answers the trust question — and is never asked it."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.asked = 0

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None

    async def trust_state(self, _session_id) -> TrustState:
        self.asked += 1
        return TrustState.AWAITING


async def _rendered_rows(launcher: _PaneIsAwaitingTrust) -> list[str]:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.application.profiles import ProfileAvailability

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=launcher,
                projects=object(),
                refresh_catalogue=tuple,
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test() as pilot:
        await app.show_detail(str(launcher.record.session_id))
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        return [str(option.prompt) for option in choices.options]


async def test_the_trust_row_is_absent_even_when_the_pane_is_showing_the_dialog() -> None:
    """The defect itself, asserted on the rendered screen rather than on a predicate.

    Read off the drawn rows for the same reason the parity contract does: `detail_entries`
    would answer this question correctly if asked with `AWAITING`, and asking it directly
    would pass whether or not the surface ever obtains that value. What is broken is the
    obtaining, so what is asserted is the screen.
    """
    launcher = _PaneIsAwaitingTrust(_awaiting_record())

    rows = await _rendered_rows(launcher)

    # Absence proves nothing about a screen that drew nothing. A detail view broken for an
    # unrelated reason renders zero rows, and "Trust this project" is not among zero rows —
    # so without this the pin below would go green on a surface that had stopped working.
    assert rows, "the session detail rendered no rows at all; the assertion below is vacuous"
    assert TRUST_ROW not in rows, (
        "the local surface now offers Trust this project. DEC-047 says it should not: the "
        "owner is looking at the dialog in the console's own pane, and a second, "
        "differently-worded route to a security answer is not a feature. If this is "
        "intended, supersede DEC-047 first and re-read the bot's twin path in the same "
        "change -- do not delete this file to make it pass."
    )


async def test_the_surface_never_even_asks_the_backend() -> None:
    """Names *why* the row is missing, so a future repair is not aimed at the wrong half.

    There are two ways this row could stay dark: the surface asks and is told UNKNOWN, or
    the surface never asks. It is the second, and the difference decides where the fix goes.
    Without this, a reader seeing only the assertion above would reasonably conclude the
    pane read was returning the wrong answer, and go looking in the tmux adapter.
    """
    launcher = _PaneIsAwaitingTrust(_awaiting_record())

    await _rendered_rows(launcher)

    assert launcher.asked == 0, (
        "the surface now reaches the backend's trust_state. Nothing on this surface should "
        "need it: the pane is on screen (DEC-040) and the dialog is answered there. If a "
        "real need has appeared, it is a decision -- supersede DEC-047"
    )


async def test_the_row_is_not_missing_because_the_policy_refuses_it() -> None:
    """The third way it could be dark, ruled out: the policy really does allow this record.

    Keeps the two assertions above from passing for a reason that has nothing to do with
    the trust row — a `claude` record the policy had stopped answering for would make them
    both green while saying nothing.
    """
    from remote_agents.application.session_actions import trust_available

    assert trust_available(_awaiting_record(), TrustState.AWAITING), (
        "the record this file pins is no longer one the policy would offer the row for, so "
        "the assertions above no longer say anything about the trust row"
    )
