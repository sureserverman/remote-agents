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
