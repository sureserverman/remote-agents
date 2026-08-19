"""Console panes and linked duplicates are invisible to lifecycle evidence.

`list-panes -a` on the shared server reports the console's own dashboard pane, and reports
every linked window a second time under the console's name (verified against tmux 3.4,
2026-08-18). Neither line is lifecycle evidence: the dashboard is not a session this
service manages, and a linked duplicate describes a pane already reported — and already
decoded — under its home session. Letting them through would either pollute the orphan
quarantine on every inventory call or, worse, double-count a session reconciliation keys
by identity. So inventory drops console-view lines before decoding, and keeps exactly one
managed pane per session identity however many times the server lists it.

The drop is deliberately narrow: a schema-less or malformed line under any *other* name is
still quarantined as orphan evidence, exactly as before — the console filter must never
become a hole real evidence can fall through (DEC-020: orphan adoption stays meaningful).
"""

from __future__ import annotations

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")


class RecordingRunner:
    def __init__(self, output: str) -> None:
        self.output = output

    async def run(self, *argv: str) -> str:
        return self.output


def managed_line(session_id: SessionId, *, pane: str = "%1") -> str:
    return "|".join(
        (f"ra-{session_id}", "$1", pane, "100", "0", "", "1", str(session_id), "proj", "claude")
    )


def console_line(*, pane: str, pid: str = "200") -> str:
    """A pane as the console session reports it: session options do not travel, so the
    schema fields arrive empty whether the pane is the dashboard or a linked duplicate."""
    return "|".join(("ra-console", "$0", pane, pid, "0", "", "", "", "", ""))


async def inventory_of(*lines: str):
    gateway = TmuxGateway("remote-agents-test-console", RecordingRunner("\n".join(lines)))
    return await gateway.inventory()


async def test_console_view_lines_are_neither_managed_nor_orphan_evidence() -> None:
    result = await inventory_of(
        managed_line(_SESSION),
        console_line(pane="%0"),  # the dashboard's own pane
        console_line(pane="%1"),  # the same managed pane, listed again under the console
    )
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert result.orphans == ()


async def test_a_schema_less_pane_under_any_other_name_is_still_quarantined() -> None:
    stray = "|".join(("ra-stray", "$9", "%9", "300", "0", "", "", "", "", ""))
    result = await inventory_of(managed_line(_SESSION), stray)
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert len(result.orphans) == 1
    assert result.orphans[0].raw == stray


async def test_one_session_yields_one_observation_however_often_it_is_listed() -> None:
    result = await inventory_of(
        managed_line(_SESSION, pane="%1"),
        managed_line(_SESSION, pane="%1"),
    )
    assert len(result.managed) == 1
    assert result.managed[0].session_id == _SESSION
    assert result.orphans == ()


async def test_an_impostor_name_carrying_the_delimiter_is_quarantined_not_dropped() -> None:
    """tmux 3.4 accepts `|` inside a session name (verified 2026-08-18). A stray session
    named `ra-console|x` mis-splits into an eleven-field line whose first field reads
    `ra-console`; the field-count check keeps it out of the console drop, so it lands in
    the orphan quarantine exactly where a stray session's line always went."""
    impostor = "|".join(("ra-console|x", "$9", "%9", "300", "0", "", "", "", "", ""))
    result = await inventory_of(managed_line(_SESSION), impostor)
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert len(result.orphans) == 1
    assert result.orphans[0].raw == impostor


async def test_a_schema_tagged_line_named_ra_console_is_quarantined_not_dropped() -> None:
    """A genuine console view always carries blank option fields — session options do not
    travel into the console's listing. A ten-field line named `ra-console` that *does*
    carry a schema tag is a fabrication, and it must fall through to parse_pane's
    quarantine rather than vanish into the console drop."""
    fabricated = "|".join(
        ("ra-console", "$9", "%9", "300", "0", "", "1", str(_SESSION), "proj", "claude")
    )
    result = await inventory_of(managed_line(_SESSION), fabricated)
    assert [pane.session_id for pane in result.managed] == [_SESSION]
    assert len(result.orphans) == 1
    assert result.orphans[0].raw == fabricated


async def test_duplicate_evidence_that_disagrees_on_liveness_is_quarantined() -> None:
    """Every session this service launches is single-window, so two valid lines for one
    identity that disagree on liveness mean someone grew a window by hand. First listed
    wins the observation; the disagreeing line becomes visible orphan evidence rather
    than being resolved silently in either direction."""
    live_line = managed_line(_SESSION)
    fields = live_line.split("|")
    fields[4] = "1"  # pane_dead: the second window's pane has died
    dead = "|".join(fields)
    result = await inventory_of(live_line, dead)
    assert len(result.managed) == 1
    assert result.managed[0].live is True
    assert len(result.orphans) == 1
    assert result.orphans[0].reason == "duplicate session evidence disagrees"
