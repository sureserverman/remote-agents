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
    """A pane as the console session reports it, carrying no mark of its own: the console
    session sets none, so the schema fields arrive empty. Under the swap model a *marked*
    pane can also be hosted by the console — that line is a displaced agent rather than a
    view, and it is covered in `test_inventory_pane_identity.py`."""
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
    """A console view carries blank option fields, so a ten-field `ra-console` line that
    does carry a **schema-1** tag is claiming a session-scoped mark the console never set —
    a fabrication or a stray, and it must fall through to parse_pane's quarantine rather
    than vanish into the console drop. Schema 2 is the deliberate exception and is not this
    test's subject: a pane-scoped mark under the console's name is a displaced agent."""
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
    than being resolved silently in either direction.

    The second line names a **different pane**, which is what "someone grew a window" means
    and what this test always intended. It used to be spelled by copying the first line and
    flipping its liveness — indistinguishable, then, from tmux listing one pane twice, and
    no longer so: a repeat listing of one pane is one pane (`test_inventory_pane_identity`)."""
    live_line = managed_line(_SESSION, pane="%1")
    fields = live_line.split("|")
    fields[2] = "%7"  # a second, hand-grown window has its own pane
    fields[4] = "1"  # pane_dead: and that pane has died
    dead = "|".join(fields)
    result = await inventory_of(live_line, dead)
    assert len(result.managed) == 1
    assert result.managed[0].live is True
    assert len(result.orphans) == 1
    assert result.orphans[0].reason == "duplicate session evidence disagrees"


def displaced_line(session_id: SessionId, *, host: str, pane: str) -> str:
    """One schema-2 pane, as listed under whichever session is showing it."""
    return "|".join((host, "$1", pane, "300", "0", "", "2", str(session_id), "proj", "claude"))


async def test_which_session_hosts_a_pane_is_not_decided_by_alphabetical_order() -> None:
    """A linked window is listed twice, and tmux emits sessions alphabetically.

    So for a pane whose window is linked into the console — which `ConsoleComposer.sync`
    does for *every* live session — the two lines are `ra-<uuid>` and `ra-console`, and
    which one a first-wins dedup keeps depends on whether the session's random UUID sorts
    before or after the literal string `console`. Roughly a quarter of UUIDs start with
    `d`, `e` or `f` and lose.

    That decides `host_session`, and `copy_attach` builds the owner's copyable command from
    it (DEC-039). Left to sort order, a session that has never been displaced hands the
    owner `attach-session -t ra-console:` — a target that resolves to the console's *current*
    window, not to their agent. Verified against tmux 3.4 (2026-08-19) by linking two
    sessions whose ids sort either side of `console` and reading the raw listing.

    The rule that removes it: a pane still listed under its **own** session name is at home,
    whatever else links its window. Only a pane absent from that listing is displaced.
    """
    low = SessionId.parse("0aaaaaaa-0000-0000-0000-000000000001")
    high = SessionId.parse("faaaaaaa-0000-0000-0000-000000000001")
    runner = RecordingRunner(
        "\n".join(
            (
                displaced_line(low, host=f"ra-{low}", pane="%1"),
                console_line(pane="%0"),
                displaced_line(low, host="ra-console", pane="%1"),
                displaced_line(high, host="ra-console", pane="%2"),
                displaced_line(high, host=f"ra-{high}", pane="%2"),
            )
        )
    )

    inventory = await TmuxGateway("remote-agents-test-hosting", runner).inventory()

    hosts = {pane.session_id: pane.session_name for pane in inventory.managed}
    assert hosts == {low: f"ra-{low}", high: f"ra-{high}"}, (
        "a session at home was reported as hosted by the console because its id sorts after "
        f"'console': {hosts}"
    )


async def test_a_pane_absent_from_its_own_window_is_reported_at_the_session_showing_it() -> None:
    """The other half, and the one the swap model needs: a genuinely displaced pane.

    An exchange leaves the agent's pane hosted by the console and *no* line under its own
    session name. Preferring the home listing must not degrade into ignoring the host, or a
    displaced agent's attach command would name a window it no longer occupies.
    """
    session_id = SessionId.parse("faaaaaaa-0000-0000-0000-000000000001")
    runner = RecordingRunner(
        "\n".join(
            (console_line(pane="%0"), displaced_line(session_id, host="ra-console", pane="%2"))
        )
    )

    inventory = await TmuxGateway("remote-agents-test-hosting", runner).inventory()

    assert [pane.session_name for pane in inventory.managed] == ["ra-console"]


async def test_two_distinct_panes_claiming_one_session_are_still_ambiguous_evidence() -> None:
    """Preferring the home listing must not swallow the DEC-020 case it sits next to.

    Deduping a repeat *listing* of one pane and quarantining two *distinct* panes are
    different jobs keyed on the same session id. A preference rule written carelessly would
    have made the second pane simply lose instead of being reported.
    """
    session_id = SessionId.parse("faaaaaaa-0000-0000-0000-000000000001")
    runner = RecordingRunner(
        "\n".join(
            (
                displaced_line(session_id, host="ra-console", pane="%2"),
                displaced_line(session_id, host=f"ra-{session_id}", pane="%9"),
            )
        )
    )

    inventory = await TmuxGateway("remote-agents-test-hosting", runner).inventory()

    assert len(inventory.managed) == 1
    assert len(inventory.orphans) == 1
    assert "disagree" in inventory.orphans[0].reason
