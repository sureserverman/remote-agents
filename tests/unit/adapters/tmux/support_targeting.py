"""A runner that answers a listing, then records what the operation under test addressed."""

from __future__ import annotations

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

_SOCKET = "remote-agents-test-target"


class TargetingRunner:
    def __init__(
        self,
        *,
        panes: tuple[tuple[str, SessionId, str], ...],
        host: str | None = None,
        capture: str = "",
        fail_with: str | None = None,
        listing_fails: str | None = None,
    ) -> None:
        self._panes = panes
        self._host = host
        self._capture = capture
        self._fail_with = fail_with
        self._listing_fails = listing_fails
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if "list-panes" in argv:
            if self._listing_fails is not None:
                raise RuntimeError(self._listing_fails)
            return "\n".join(
                "|".join(
                    (
                        self._host if self._host is not None else f"ra-{session_id}",
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
                for pane, session_id, schema in self._panes
            )
        if self._fail_with is not None:
            raise RuntimeError(self._fail_with)
        return self._capture

    @property
    def listings(self) -> int:
        return len([call for call in self.calls if "list-panes" in call])

    @property
    def capture_call(self) -> tuple[str, ...]:
        return next(call for call in self.calls if "capture-pane" in call)

    @property
    def key_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if "send-keys" in call]


def gateway_for(runner: TargetingRunner) -> TmuxGateway:
    return TmuxGateway(_SOCKET, runner)
