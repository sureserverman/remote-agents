"""Inventory reads identity off the pane, wherever tmux happens to be listing it.

Under the swap model a managed pane can be hosted by the console, so "which session is
this line under" stops being an identity question and becomes a location one. Three rules
follow, and each is a way inventory could go wrong instead:

- A schema-2 pane decodes to the same session whether it is at home or in the console.
- A console line is presentation only when it carries **no** managed mark. A marked pane
  hosted by the console is the agent itself, and dropping it would hide a live session.
- One pane listed twice is one pane. tmux re-reports a linked window under the console's
  name, and a pane-scoped mark is intrinsic to the pane, so both listings now carry the
  identity — which is a repeat, never the two-windows-disagreeing case the orphan
  quarantine exists for.

The legacy shape keeps working throughout: a schema-1 session carries its mark on the
session, its pane inherits it by tmux's own fallback, and nothing about that changed.
"""

from __future__ import annotations

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_OTHER = SessionId.parse("fedcba98-7654-3210-fedc-ba9876543210")


class RecordingRunner:
    def __init__(self, output: str) -> None:
        self.output = output

    async def run(self, *argv: str) -> str:
        return self.output


def line(
    session_id: SessionId,
    *,
    host: str | None = None,
    pane: str = "%1",
    schema: str = "2",
    dead: str = "0",
) -> str:
    return "|".join(
        (
            host if host is not None else f"ra-{session_id}",
            "$1",
            pane,
            "100",
            dead,
            "",
            schema,
            str(session_id),
            "opaque-editor",
            "claude",
        )
    )


def console_line(*, pane: str) -> str:
    """The console's own surface pane: ten fields, no mark of any kind."""
    return "|".join(("ra-console", "$0", pane, "200", "0", "", "", "", "", ""))


async def inventory_of(*lines: str):
    return await TmuxGateway(
        "remote-agents-test-pane", RecordingRunner("\n".join(lines))
    ).inventory()


async def test_a_pane_at_home_decodes_to_its_session() -> None:
    result = await inventory_of(line(_SESSION))
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert result.managed[0].session_name == f"ra-{_SESSION}"
    assert result.orphans == ()


async def test_the_same_pane_hosted_by_the_console_decodes_identically() -> None:
    """The swap's whole premise. Only the host differs; the session does not."""
    home = (await inventory_of(line(_SESSION))).managed[0]
    displaced = (await inventory_of(line(_SESSION, host="ra-console"))).managed[0]

    assert displaced.session_id == home.session_id
    assert displaced.pane_id == home.pane_id
    assert (displaced.project_id, displaced.profile_id) == (home.project_id, home.profile_id)
    assert (displaced.live, displaced.preserved) == (home.live, home.preserved)
    assert displaced.session_name == "ra-console"


async def test_a_marked_pane_in_the_console_is_evidence_not_noise() -> None:
    """The narrowing: dropping this line would report a running agent as gone."""
    result = await inventory_of(line(_SESSION, host="ra-console"), console_line(pane="%0"))
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert result.orphans == ()


async def test_an_unmarked_console_line_is_still_dropped() -> None:
    result = await inventory_of(console_line(pane="%0"), console_line(pane="%5"))
    assert result.managed == ()
    assert result.orphans == ()


async def test_a_legacy_session_still_decodes() -> None:
    result = await inventory_of(line(_SESSION, schema="1"))
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert result.orphans == ()


async def test_a_legacy_mark_under_a_foreign_host_is_still_quarantined() -> None:
    """A schema-1 mark reaching a line under another name is inheritance, not identity —
    the console has no session options of its own, so this is a fabrication or a stray."""
    stray = line(_SESSION, host="ra-console", schema="1")
    result = await inventory_of(stray)
    assert result.managed == ()
    assert [orphan.raw for orphan in result.orphans] == [stray]


async def test_one_pane_listed_twice_is_one_pane_not_a_disagreement() -> None:
    """A linked window is re-reported under the console, and a pane-scoped mark travels
    into that re-listing — so both lines now carry the identity where only one used to.
    Same pane id means same pane: a repeat, dropped, never orphan evidence of a second
    window. A pane cannot be in two states, so a difference here is a listing artifact."""
    result = await inventory_of(
        line(_SESSION, pane="%4"),
        line(_SESSION, host="ra-console", pane="%4", dead="1"),
    )
    assert len(result.managed) == 1
    assert result.managed[0].pane_id == "%4"
    assert result.managed[0].live is True
    assert result.orphans == ()


async def test_a_different_pane_claiming_one_identity_is_still_ambiguous() -> None:
    """The rule the repeat-drop must not swallow: two *distinct* panes disagreeing about
    one session is somebody's hand-grown second window, and it stays visible (DEC-020)."""
    second = line(_SESSION, pane="%9", dead="1")
    result = await inventory_of(line(_SESSION, pane="%4"), second)
    assert len(result.managed) == 1
    assert result.managed[0].pane_id == "%4"
    assert [orphan.reason for orphan in result.orphans] == ["duplicate session evidence disagrees"]


async def test_distinct_sessions_are_untouched_by_any_of_this() -> None:
    result = await inventory_of(line(_SESSION), line(_OTHER, host="ra-console"))
    assert {pane.session_id for pane in result.managed} == {_SESSION, _OTHER}
    assert result.orphans == ()
