"""What tmux itself does with a console status line, measured before anything is built on it.

The function-key bar (DEC-105) is one `status-format[0]` string, and three things it depends on
are tmux's behaviour rather than this project's code. Each is asserted here on a real tmux
client, so a tmux that behaves differently fails a named test instead of drawing a wrong bar:

1. `#[align=right]` inside `status-format[0]` puts the right-hand text in the last columns.
2. Setting a user option the format reads redraws the status row without waiting for
   `status-interval` — so the bar can follow the sessions cursor with the interval at 0.
3. `#{e|>|:#{client_width},160}` compares **numbers**. `#{>:…}` compares strings, and on tmux
   3.4 `#{>:99,160}` is `1`, so a 99-column client would be handed the 200-column bar.

**The client is real, and sized by a pane rather than by a pty ioctl.** An outer scratch server
runs `tmux attach` for the inner one inside a pane of the size under test, and that pane is
captured. `client_width` is then the width a terminal of that size reports, and the captured
last row is exactly what the owner's terminal would show — status line included, which a
`capture-pane` of the inner server can never include.

**One more fact, found by this probe and relied on by the gateway:** `status`, `status-style`
and `status-format` are **session** options. `set-option -w -t ra-console:` does not refuse them
on tmux 3.4 — it lands them on that session, and a second session on the same server keeps the
default bar. So the bar is scoped to the console session, never server-wide.
"""

from __future__ import annotations

import os
import subprocess
import time
from uuid import uuid4

import pytest

#: How long a redraw may take before the assertion is that it did not happen.
_REDRAW_BOUND_SECONDS = 1.0

#: The bound the gateway's `status-interval 0` relies on; measured, printed, and asserted.
_PROMPT_REDRAW_SECONDS = 0.1

#: Real tmux clients: one server pair per test, serially — see `run-the-baseline-alone`.
pytestmark = pytest.mark.skipif(
    os.environ.get("PYTEST_XDIST_WORKER") is not None,
    reason="attaches real nested tmux clients; timed redraws run alone",
)


class _NestedClient:
    """An inner scratch server whose one session is shown by a client inside an outer pane."""

    def __init__(self, width: int, height: int = 10) -> None:
        tag = uuid4().hex
        self.inner = f"remote-agents-probe-in-{tag}"
        self.outer = f"remote-agents-probe-out-{tag}"
        self._size = (width, height)

    def inner_tmux(self, *args: str) -> str:
        return _tmux(self.inner, *args)

    def start(self, status_format: str, *, interval: int) -> None:
        self.inner_tmux("new-session", "-d", "-s", "probe", "-x", "80", "-y", "24", "sleep 600")
        self.inner_tmux("set-option", "-w", "-t", "probe:", "status-interval", str(interval))
        self.inner_tmux("set-option", "-w", "-t", "probe:", "status-format[0]", status_format)
        width, height = self._size
        # `env -u TMUX`: the suite may itself run inside tmux, and a nested attach refuses then.
        attach = f"env -u TMUX tmux -L {self.inner} attach-session -t probe:"
        _tmux(
            self.outer, "new-session", "-d", "-s", "o", "-x", str(width), "-y", str(height), attach
        )
        assert _wait_for(lambda: self.status_row().strip() != ""), "the nested client never drew"

    def resize(self, width: int) -> None:
        _tmux(self.outer, "resize-window", "-t", "o:", "-x", str(width))

    def status_row(self) -> str:
        """The outer pane's last row: the inner client's status line, as its terminal shows it."""
        screen = _tmux(self.outer, "capture-pane", "-p", "-t", "o:")
        return screen.rstrip("\n").split("\n")[-1]

    def close(self) -> None:
        for socket in (self.outer, self.inner):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)


def _tmux(socket: str, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-L", socket, *args], capture_output=True, text=True, check=True
    ).stdout


def _wait_for(ready, bound: float = 5.0) -> bool:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def nested():
    made: list[_NestedClient] = []

    def make(width: int) -> _NestedClient:
        client = _NestedClient(width)
        made.append(client)
        return client

    yield make
    for client in made:
        client.close()


def test_align_right_puts_the_right_text_in_the_last_columns(nested) -> None:
    client = nested(100)
    client.start("keys#[align=right]RIGHT", interval=0)

    row = client.status_row()

    assert row.startswith("keys")
    assert len(row) == 100
    assert row.endswith("RIGHT"), repr(row)


def test_a_user_option_change_redraws_the_status_row(nested) -> None:
    client = nested(100)
    client.start("x=#{@x}", interval=1)
    before = client.status_row()

    client.inner_tmux("set-option", "-w", "-t", "probe:", "@x", "1")

    assert _wait_for(lambda: client.status_row().startswith("x=1"), _REDRAW_BOUND_SECONDS)
    assert before.startswith("x=") and not before.startswith("x=1")


def test_a_user_option_change_redraws_at_once_with_no_interval(nested) -> None:
    """The recorded fact that lets the gateway set `status-interval 0` (Task 2.1).

    With the interval at 0 tmux never redraws on a timer, so a change seen here within 100 ms
    is the option change itself causing the redraw.
    """
    client = nested(100)
    client.start("x=#{@x}", interval=0)

    started = time.monotonic()
    client.inner_tmux("set-option", "-w", "-t", "probe:", "@x", "1")
    redrew = _wait_for(lambda: client.status_row().startswith("x=1"), _PROMPT_REDRAW_SECONDS)
    elapsed = time.monotonic() - started

    print(f"redraw on option change, status-interval 0: {redrew} after {elapsed * 1000:.0f} ms")
    assert redrew


@pytest.mark.parametrize(
    ("width", "variant"),
    [(99, "COMPACT"), (100, "COMPACT"), (160, "COMPACT"), (161, "FULL"), (200, "FULL")],
)
def test_the_numeric_width_switch_flips_above_160(nested, width: int, variant: str) -> None:
    """99 is the case that matters: a string compare agrees with a numeric one at 100–200."""
    client = nested(width)
    client.start("#{?#{e|>|:#{client_width},160},FULL,COMPACT}", interval=0)

    assert client.status_row().strip() == variant


def test_the_string_compare_is_what_the_numeric_switch_avoids() -> None:
    """The measurement behind R4, kept so the reason survives the README it corrects."""
    socket = f"remote-agents-probe-cmp-{uuid4().hex}"
    try:
        _tmux(socket, "new-session", "-d", "-s", "cmp", "sleep 600")
        as_strings = _tmux(socket, "display-message", "-p", "#{>:99,160}").strip()
        as_numbers = _tmux(socket, "display-message", "-p", "#{e|>|:99,160}").strip()
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)

    assert (as_strings, as_numbers) == ("1", "0")


def test_status_options_set_with_w_stay_on_the_one_session() -> None:
    socket = f"remote-agents-probe-scope-{uuid4().hex}"
    try:
        _tmux(socket, "new-session", "-d", "-s", "console", "sleep 600")
        _tmux(socket, "new-session", "-d", "-s", "agent", "sleep 600")
        _tmux(socket, "set-option", "-w", "-t", "console:", "status-format[0]", "BAR")

        console = _tmux(socket, "show-options", "-v", "-t", "console:", "status-format[0]")
        agent = _tmux(socket, "show-options", "-v", "-t", "agent:", "status-format[0]")
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)

    assert console.strip() == "BAR"
    assert agent.strip() == ""
