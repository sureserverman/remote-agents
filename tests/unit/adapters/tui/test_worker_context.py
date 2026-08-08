"""`push_screen_wait` has a worker to run in, proved by running it.

Sub-plan 2's Preflight asserts this precondition, and sub-plan 1 did not leave it: DEC-008
withdrew the `@work` migration, so every blocking call moved onto `run_worker(thread=True)`
helpers whose *callers* still await from the message pump. `push_screen_wait` refuses that —
it calls `get_current_worker()` and raises `NoActiveWorker` when there is none
(`textual/app.py:2958-2964`).

The distinction this file pins is one a grep for `@work` cannot make: that the decorator is
on a method whose body is where the modal is awaited. A refactor that moved the
`push_screen_wait` call out to its caller would keep the decorator and break the property,
and Stage 3 is where an unconfirmed force stop would be the cost.
"""

from __future__ import annotations

import pytest
from test_tui_snapshots import settle
from test_tui_worker_exclusivity import _context, _SlowLauncher
from textual.screen import ModalScreen
from textual.widgets import Label
from textual.worker import NoActiveWorker, get_current_worker

from remote_agents.adapters.tui.app import RemoteAgentsTui


class _Answer(ModalScreen[bool]):
    """A modal that dismisses itself with a fixed answer as soon as it is mounted."""

    def __init__(self, answer: bool) -> None:
        super().__init__()
        self._answer = answer

    def compose(self):
        yield Label("answer?", markup=False)

    def on_mount(self) -> None:
        self.dismiss(self._answer)


@pytest.mark.parametrize("answer", [True, False])
async def test_ask_returns_what_the_modal_was_dismissed_with(answer: bool) -> None:
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        assert await app._ask(_Answer(answer)).wait() is answer


async def test_the_pump_itself_is_not_a_worker_context() -> None:
    """The negative half: without `_ask`, awaiting a modal from a handler raises.

    Without this, `_ask` could be deleted in favour of a direct `push_screen_wait` and the
    positive test above would still pass on some future Textual that dropped the check.
    """
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        with pytest.raises(NoActiveWorker):
            get_current_worker()
        with pytest.raises(NoActiveWorker):
            await app.push_screen_wait(_Answer(True))


async def test_ask_does_not_cancel_a_confirmation_already_in_flight() -> None:
    """DEC-008 under the mechanism Stage 3 will use.

    `exclusive=True` would cancel the running worker and start a new one — on a force-stop
    confirmation that means an unanswered modal dismissed and a second kill issued. Two
    overlapping asks must therefore both resolve, not one.
    """
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        first = app._ask(_Answer(True))
        second = app._ask(_Answer(False))
        assert await first.wait() is True
        assert await second.wait() is False
