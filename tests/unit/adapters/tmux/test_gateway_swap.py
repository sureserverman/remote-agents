"""The exchange is between two panes, and it may name nothing else.

`swap-pane` is the one operation in this adapter that moves an agent's pane out of the
window it was launched in, so both of its targets are agent-reaching addresses and both go
through `exact_pane_target` (DEC-001, DEC-038). A session target here would be worse than
elsewhere rather than merely imprecise: `ra-<uuid>:` resolves to whichever pane occupies
that window now, so one wrong end of an exchange puts an agent into a stranger's window and
crosses two identities — the failure the composer's two-swap rule exists to avoid, arriving
one layer below it.

`-d` is asserted, not incidental: focus is presentation policy the surface owns, and a
swap that also moved the client would make a background recovery unwind visible as a focus
jump. Probed on tmux 3.4 (2026-08-19): without `-d` the target position becomes active,
with `-d` the previously active pane stays active.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.ports.terminal import TerminalTargetMissing

_BASE = ("tmux", "-L", "remote-agents-test-swap")


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
    return TmuxGateway("remote-agents-test-swap", runner)


async def test_swap_panes_exchanges_two_pane_ids_without_moving_the_client() -> None:
    runner = RecordingRunner()

    await gateway(runner).swap_panes("%7", "%3")

    assert runner.calls == [(*_BASE, "swap-pane", "-d", "-s", "%7", "-t", "%3")]


@pytest.mark.parametrize(
    "source, target",
    [
        ("ra-01234567-89ab-cdef-0123-456789abcdef:", "%3"),
        ("%7", "ra-01234567-89ab-cdef-0123-456789abcdef:"),
        ("%7", "ra-console:"),
        ("", "%3"),
        ("%7", ""),
        ("%7", "%"),
        ("%7", "%3 ; kill-server"),
        ("%-1", "%3"),
    ],
)
async def test_swap_panes_refuses_anything_that_is_not_a_decoded_pane_id(
    source: str, target: str
) -> None:
    """Both ends, not just the source: an exchange has two agent-reaching addresses.

    The session-shaped rows are the ones that matter. A window target on either end silently
    swaps whatever happens to occupy that window, which is how an agent lands in another
    session's home and two identities cross.
    """
    runner = RecordingRunner()

    with pytest.raises(ValueError):
        await gateway(runner).swap_panes(source, target)

    assert runner.calls == [], "a refused target must not reach the runner"


async def test_a_pane_that_has_gone_raises_the_same_typed_error_as_every_single_target() -> None:
    runner = RecordingRunner(error=RuntimeError("can't find pane: %7"))

    with pytest.raises(TerminalTargetMissing):
        await gateway(runner).swap_panes("%7", "%3")


async def test_a_broken_tmux_stays_a_broken_tmux_rather_than_becoming_a_missing_pane() -> None:
    """The other half of the retyping, which the missing-pane case alone would not pin.

    A composer that treats every failure as "already gone" would report a console it never
    repaired as a console with nothing to repair.
    """
    runner = RecordingRunner(error=RuntimeError("server exited unexpectedly"))

    with pytest.raises(RuntimeError) as raised:
        await gateway(runner).swap_panes("%7", "%3")

    assert not isinstance(raised.value, TerminalTargetMissing)
