"""One identity resolves to the pane currently carrying it, and never to a remembered one.

The pane moves. That is the whole premise of the console this sub-plan is groundwork for,
and it is why resolution is a call rather than a field: an answer cached at launch is
correct until the first swap and silently wrong forever after. Every pane-following
operation asks again.

`None` is a real answer with two causes that deliberately share it — a session launched
before schema 2, whose identity lives on its session and has no pane mark to find, and a
session that is simply gone. Callers fall back to the session target for the first and
find nothing for the second, and both are the same instruction: do not address a pane you
could not resolve.
"""

from __future__ import annotations

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_OTHER = SessionId.parse("fedcba98-7654-3210-fedc-ba9876543210")


class RecordingRunner:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        return self.output


def line(session_id: SessionId, *, host: str | None = None, pane: str = "%1", schema: str = "2"):
    return "|".join(
        (
            host if host is not None else f"ra-{session_id}",
            "$1",
            pane,
            "100",
            "0",
            "",
            schema,
            str(session_id),
            "opaque-editor",
            "claude",
        )
    )


def gateway(*lines: str) -> TmuxGateway:
    return TmuxGateway("remote-agents-test-resolve", RecordingRunner("\n".join(lines)))


async def test_a_pane_at_home_resolves_to_its_own_id() -> None:
    assert await gateway(line(_SESSION, pane="%3")).pane_for(_SESSION) == "%3"


async def test_the_same_identity_resolves_when_the_pane_is_hosted_elsewhere() -> None:
    """The case session addressing gets wrong, and the only reason this method exists."""
    displaced = gateway(line(_SESSION, host="ra-console", pane="%3"))
    assert await displaced.pane_for(_SESSION) == "%3"


async def test_an_unknown_identity_resolves_to_nothing() -> None:
    assert await gateway(line(_OTHER)).pane_for(_SESSION) is None


async def test_an_empty_server_resolves_to_nothing() -> None:
    assert await gateway().pane_for(_SESSION) is None


async def test_a_legacy_session_resolves_to_nothing_because_it_marks_no_pane() -> None:
    """A schema-1 session decodes — its pane inherits the session's mark — but that mark is
    not evidence about *which pane*, so it cannot be addressed as one. Answering `None`
    here is what routes such a session to the session target it has always used."""
    assert await gateway(line(_SESSION, schema="1")).pane_for(_SESSION) is None


async def test_the_resolved_pane_is_a_valid_pane_target() -> None:
    """Resolution feeds argv, so what it returns has to satisfy the codec's closed shape —
    it is a decoded id from our own inventory, which is exactly what DEC-001 permits."""
    from remote_agents.adapters.tmux.codec import exact_pane_target

    resolved = await gateway(line(_SESSION, pane="%12")).pane_for(_SESSION)
    assert resolved is not None
    assert exact_pane_target(resolved) == "%12"


async def test_resolution_reads_the_server_every_time_it_is_asked() -> None:
    """No caching, because the pane moves. Two asks are two listings."""
    runner = RecordingRunner(line(_SESSION, pane="%3"))
    resolver = TmuxGateway("remote-agents-test-resolve", runner)

    await resolver.pane_for(_SESSION)
    await resolver.pane_for(_SESSION)

    assert [call[3] for call in runner.calls] == ["list-panes", "list-panes"]


async def test_a_preserved_pane_still_resolves() -> None:
    """Liveness is not part of resolution, and this pins that on purpose.

    A PRESERVED pane is dead but present, and its retained output is the whole reason the
    state exists (DEC-021) — so `capture` has to be able to reach it. What the caller
    inherits is the obligation to ask about liveness itself before *typing*: tmux answers
    `send-keys` at a dead pane with exit 0 and no effect (verified, tmux 3.4), so an
    unchecked graceful stop would report a keystroke it never delivered (DEC-022).
    """
    dead = "|".join((f"ra-{_SESSION}", "$1", "%5", "100", "1", "", "2", str(_SESSION), "p", "c"))
    resolver = gateway(dead)

    assert await resolver.pane_for(_SESSION) == "%5"
    inventory = await resolver.inventory()
    assert (inventory.managed[0].live, inventory.managed[0].preserved) == (False, True)
