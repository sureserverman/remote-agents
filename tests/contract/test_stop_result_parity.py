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

So there are three claims here and they fail separately:

* each surface **names the cause** — the fix on that surface;
* the two surfaces **agree** — DEC-007, and the thing a single shared vocabulary buys;
* the two causes **do not read alike** on either surface — the half BL-008 would otherwise
  close without answering, since one message for both causes satisfies the first two claims.

The detail values are spelled as literals rather than imported as constants, deliberately.
They are the strings `adapters/tmux/runtime.py` puts on the wire, and a contract test that
imported the constants would keep passing if a rename changed what the adapter actually emits.
`test_the_constants_still_name_the_wire_values` pins the two together in one place instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import unescape

import pytest
from textual.widgets import OptionList

from remote_agents.adapters.telegram.service import PrivateBotBoundary
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


class _Launcher:
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
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject(str(_PROJECT_ID), "opaque-editor", "tests", "Registered"),),
        launcher=launcher,
    )
    boundary._view_revisions[(7, 11)] = 1
    token = boundary.stops.offer(
        record.session_id, record.profile_id, record.state, "graceful", 7, 11, 1
    )
    assert token is not None, "the bot offered no graceful stop, so nothing was exercised"
    reply = await boundary._stop_reply("graceful", token)
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
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    launcher = _Launcher(record, detail)
    app = RemoteAgentsTui(
        TuiContext(
            launcher=launcher,  # type: ignore[arg-type]
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=tuple,
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


SURFACES = (
    ("telegram", _telegram_said),
    ("tui", _tui_said),
)


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
