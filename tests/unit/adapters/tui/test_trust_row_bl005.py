"""BL-005: the local surface never offers "Trust this project", and that is left as found.

DEC-016 decided that both surfaces answer the folder-trust question, and both were built
to: the row, its handler, and the command all exist in `screens/sessions.py`. What does not
exist is the field it reads. `_observed_trust` asks the **context** for a trust state, and
`SessionService.trust_state` is a backend method — the context has never carried one — so
the read always comes back empty, the state is always UNKNOWN, and `trust_available` always
says no. The row has therefore never rendered on this surface, on any session, since it was
written.

**It is deliberately not repaired here.** Typing `TuiContext` against `Backend` is exactly
the change that fixes it by accident: the backend does have `sessions.trust_state`, and one
plausible line would light the row up. The owner's decision on 2026-08-21 was that this
refactor changes no functionality, so a defect it happens to sit on top of is not repaired
as a side effect — that is a separate decision, taken separately, with the bot's twin path
re-read at the same time.

So this file exists to make the defect *hold*. It asserts the row is absent for the one
record that would earn it — a `claude` session whose pane really is showing the dialog —
which is a test that fails the moment anybody repairs BL-005, deliberately or otherwise.
**When BL-005 is fixed, this file is deleted**, and the backlog entry says so. A test
pinning a defect is only honest while the defect is a decision; the day it stops being one,
this is the thing standing in the way.
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
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    app = RemoteAgentsTui(
        TuiContext(
            backend=backend_for(
                sessions=launcher,
                projects=object(),
                refresh_catalogue=tuple,
            ),
            profiles=(ProfileChoice("claude", True),),
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

    assert TRUST_ROW not in rows, (
        "BL-005 has been repaired. That is a decision, not a refactor: delete this file, "
        "close BL-005, and re-read the bot's twin path in the same change (DEC-016)."
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
        "the surface now reaches the backend's trust_state; if that is intended, BL-005 is "
        "fixed and this file should be deleted rather than adjusted"
    )


async def test_the_row_is_not_missing_because_the_policy_refuses_it() -> None:
    """The third way it could be dark, ruled out: the policy really does allow this record.

    Keeps the two assertions above from passing for a reason that has nothing to do with
    BL-005 — a `claude` record the policy had stopped answering for would make them both
    green while saying nothing.
    """
    from remote_agents.application.session_actions import trust_available

    assert trust_available(_awaiting_record(), TrustState.AWAITING), (
        "the record this file pins is no longer one the policy would offer the row for, so "
        "the assertions above no longer say anything about BL-005"
    )
