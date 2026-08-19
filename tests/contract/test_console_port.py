"""The gateway satisfies ConsolePort exactly — drift fails here, not in production logs.

The composer deliberately swallows console exceptions (DEC-006), which makes a signature
mismatch between the port and the gateway indistinguishable, at runtime, from ordinary
console unavailability: every call would degrade to a "sync failed" log line. This pins
the structural contract loudly instead — presence via the runtime-checkable protocol, and
exact signatures via inspect, parameter names and order included, because a reordered
parameter is precisely the drift a presence check cannot see.
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


def test_every_port_method_matches_the_gateway_signature_exactly() -> None:
    gateway = _gateway()
    for name in (
        "console_exists",
        "create_console",
        "install_console_binding",
        "console_windows",
        "link_session_window",
        "unlink_console_window",
        "select_console_window",
        "switch_client_to_session",
        "console_active_window",
        "display_message",
    ):
        port_signature = inspect.signature(getattr(ConsolePort, name))
        gateway_signature = inspect.signature(type(gateway).__dict__[name])
        assert port_signature == gateway_signature, name
