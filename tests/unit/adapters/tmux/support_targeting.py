"""A runner that answers a listing, then records what the operation under test addressed.

`fail_after_first_kill` simulates a *race*: some later kill finds its pane already gone. It
does not model a guaranteed cascade — killing the first of two panes in a window does not
destroy that window, only killing its last one does — so the knob is named for the shape it
produces rather than for a tmux mechanism it would be overclaiming.

Deliberately built out of real `PANE_FORMAT` lines rather than pre-decoded objects, so the
tests exercise the actual `inventory()`/`parse_pane` path. A fake that returned `ManagedPane`
values directly would let a targeting test pass while the decode it depends on was wrong.

The listing failure and the operation failure are separate knobs, and that separation is the
point: a fixture that fails every call with one message can only ever prove the symmetric
case, while the dangerous one is asymmetric — the listing fails and the operation would have
succeeded, against whatever pane now occupies the window.
"""

from __future__ import annotations

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

_SOCKET = "remote-agents-test-target"


def pane_line(pane: str, session_id: SessionId, schema: str, host: str | None = None) -> str:
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


class TargetingRunner:
    def __init__(
        self,
        *,
        panes: tuple[tuple[str, SessionId, str], ...],
        host: str | None = None,
        capture: str = "",
        fail_with: str | None = None,
        listing_fails: str | None = None,
        extra_lines: tuple[str, ...] = (),
        fail_after_first_kill: str | None = None,
    ) -> None:
        self._panes = panes
        self._host = host
        self._capture = capture
        self._fail_with = fail_with
        self._listing_fails = listing_fails
        self._extra_lines = extra_lines
        self._fail_after_first_kill = fail_after_first_kill
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if "list-panes" in argv:
            if self._listing_fails is not None:
                raise RuntimeError(self._listing_fails)
            lines = [
                pane_line(pane, session_id, schema, self._host)
                for pane, session_id, schema in self._panes
            ]
            return "\n".join([*lines, *self._extra_lines])
        if self._fail_after_first_kill is not None and "kill-pane" in argv:
            if len([c for c in self.calls if "kill-pane" in c]) > 1:
                raise RuntimeError(self._fail_after_first_kill)
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
