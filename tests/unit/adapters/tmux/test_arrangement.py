"""Where a pane is shown and whose it is are two decodings, and they must not merge.

The arrangement read is what the swap composer sees. Its whole value is that host and
identity can *disagree* — that is what "displaced" means — so a decoder that collapsed them
would report a resting console in every state, including the broken ones.

The trap is tmux's pane -> session fallback on `#{@option}`. In a schema-1 session's window
every pane reports that session's id, including a console surface parked there by an
exchange. Read as identity, that surface *is* the agent; read as hosting, it is a pane
sitting in that session's window, which is what the host field already says. Only a
schema-2 mark is the pane's own (DEC-038).
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import ARRANGEMENT_FORMAT, parse_arrangement
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId
from remote_agents.ports.console import HostedPane

_A = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_BASE = ("tmux", "-L", "remote-agents-test-arrange")


class RecordingRunner:
    def __init__(self, output: str = "", error: RuntimeError | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        return self.output


def gateway(runner: RecordingRunner) -> TmuxGateway:
    return TmuxGateway("remote-agents-test-arrange", runner)


def test_a_displaced_agent_reads_as_hosted_by_the_console_and_owned_by_itself() -> None:
    host, on_console, window, position, pane, identity = parse_arrangement(
        f"ra-console|0|0|%2|2|{_A}"
    )

    assert (host, on_console, window, position, pane, identity) == (None, True, 0, 0, "%2", _A)


def test_the_pane_it_displaced_reads_as_hosted_by_that_session_and_owned_by_nobody() -> None:
    """The console surface parked in an agent's window: hosted there, identity none.

    Its `host` is what lets the composer find it again — it is the pane in the displaced
    agent's own window — and its empty identity is what stops it being mistaken for the
    agent by anything that looks for one.
    """
    host, on_console, _window, _position, pane, identity = parse_arrangement(f"ra-{_A}|0|0|%1||")

    assert (host, on_console, pane, identity) == (_A, False, "%1", None)


def test_an_inherited_schema_one_mark_is_hosting_and_never_identity() -> None:
    """tmux reports the session's mark on every pane in its window; only schema 2 is the pane's."""
    host, _on_console, _window, _position, _pane, identity = parse_arrangement(
        f"ra-{_A}|0|1|%9|1|{_A}"
    )

    assert host == _A
    assert identity is None, "an inherited mark was read as the pane's own identity"


def test_a_pane_in_nobodys_managed_window_has_neither_a_host_nor_the_console() -> None:
    host, on_console, _window, _position, _pane, identity = parse_arrangement("scratch|0|0|%4||")

    assert (host, on_console, identity) == (None, False, None)


@pytest.mark.parametrize(
    "line",
    [
        "ra-console|0|0|%2|2",
        "ra-console|x|0|%2||",
        "ra-console|0|-1|%2||",
        "ra-console|0|0|||",
        "ra-console|0|0|%2|2|not-a-uuid",
    ],
)
def test_a_line_that_cannot_be_decoded_is_refused_rather_than_guessed(line: str) -> None:
    with pytest.raises(ValueError):
        parse_arrangement(line)


def test_a_session_name_carrying_the_delimiter_cannot_impersonate_the_console() -> None:
    """tmux 3.4 accepts `|` inside a session name (Claim 3), and the format uses `|`.

    Split naively, `ra-console|x` yields a first field reading `ra-console` — a stray session
    presenting itself as the console, whose left slot the composer would then exchange
    against. The field count is what keeps it out: the embedded delimiter inflates the split
    past six, so the line is refused here and dropped by the gateway.
    """
    with pytest.raises(ValueError):
        parse_arrangement("ra-console|x|0|0|%2||")


async def test_the_gateway_asks_for_every_pane_on_the_server_and_names_no_session() -> None:
    runner = RecordingRunner(output=f"ra-console|0|0|%2|2|{_A}\nra-{_A}|0|0|%1||\n")

    arrangement = await gateway(runner).pane_arrangement()

    assert runner.calls == [(*_BASE, "list-panes", "-a", "-F", ARRANGEMENT_FORMAT)]
    assert ARRANGEMENT_FORMAT.startswith("#{session_name}|#{window_index}|#{pane_index}"), (
        "the format's field order is what parse_arrangement unpacks positionally"
    )
    assert arrangement == (
        HostedPane(None, True, 0, 0, "%2", _A),
        HostedPane(_A, False, 0, 0, "%1", None),
    )


async def test_a_line_the_decoder_refuses_costs_one_pane_rather_than_the_whole_reading() -> None:
    """Dropped, not quarantined — the opposite of `inventory`, deliberately.

    A malformed line here costs the composer one pane it will not move, which the owner can
    see and act on. In `inventory` the same line is a session whose state nobody can explain,
    which is why that path keeps it as evidence (DEC-020). Same server, different question.
    """
    runner = RecordingRunner(output=f"garbage\nra-console|0|0|%2|2|{_A}\n|||||\n")

    assert await gateway(runner).pane_arrangement() == (HostedPane(None, True, 0, 0, "%2", _A),)


async def test_an_absent_server_is_an_empty_arrangement_rather_than_a_failure() -> None:
    runner = RecordingRunner(error=RuntimeError("no server running on /tmp/tmux-1000/x"))

    assert await gateway(runner).pane_arrangement() == ()


async def test_a_broken_server_is_raised_rather_than_reported_as_nothing_being_shown() -> None:
    runner = RecordingRunner(error=RuntimeError("server exited unexpectedly"))

    with pytest.raises(RuntimeError):
        await gateway(runner).pane_arrangement()
