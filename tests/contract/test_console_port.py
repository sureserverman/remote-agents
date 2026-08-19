"""The gateway satisfies ConsolePort exactly — drift fails here, not in production logs.

The composer deliberately swallows console exceptions (DEC-006), which makes a signature
mismatch between the port and the gateway indistinguishable, at runtime, from ordinary
console unavailability: every call would degrade to a "sync failed" log line. This pins
the structural contract loudly instead — presence via the runtime-checkable protocol, and
exact signatures via inspect, parameter names and order included, because a reordered
parameter is precisely the drift a presence check cannot see.

The set of methods is **derived from the protocol**, not listed here. Listed, it went stale
the first time the port grew — see `_port_methods`.
"""

from __future__ import annotations

import inspect

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.ports.console import ConsolePort


def _gateway() -> TmuxGateway:
    class _Runner:
        async def run(self, *argv: str) -> str:
            return ""

    return TmuxGateway("remote-agents-test-contract", _Runner())


def test_the_gateway_is_a_console_port() -> None:
    assert isinstance(_gateway(), ConsolePort)


def _port_methods() -> tuple[str, ...]:
    """Every public method the protocol declares, read from the protocol itself.

    This used to be a hand-written tuple, and it went stale the first time the port grew:
    `pane_arrangement` and `swap_panes` were added for the swap composer and nobody thought
    to extend the list, so the two newest methods — the ones most likely to drift — were the
    two the parity check did not cover. A list that must be edited in step with the thing it
    checks is a list that will not be. Derived, it cannot go stale.
    """
    return tuple(
        name
        for name, value in vars(ConsolePort).items()
        if not name.startswith("_") and callable(value)
    )


def test_the_derivation_finds_the_ports_methods_at_all() -> None:
    """Guards the check above from passing over an empty set.

    A derivation that silently found nothing would turn the parity test into a loop over no
    names — green, and proving exactly nothing, which is the failure mode the hand-written
    tuple at least could not have.
    """
    methods = _port_methods()

    assert len(methods) >= 10, methods
    assert {"pane_arrangement", "swap_panes", "console_windows"} <= set(methods)


def test_every_port_method_matches_the_gateway_signature_exactly() -> None:
    gateway = _gateway()
    for name in _port_methods():
        assert name in type(gateway).__dict__, (
            f"the gateway does not implement {name}, which ConsolePort declares"
        )
        port_signature = inspect.signature(getattr(ConsolePort, name))
        gateway_signature = inspect.signature(type(gateway).__dict__[name])
        assert port_signature == gateway_signature, name
