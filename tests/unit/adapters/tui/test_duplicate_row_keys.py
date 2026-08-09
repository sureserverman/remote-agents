"""A repeated row key drops a row; it never takes the screen down.

`OptionList.add_options` raises `DuplicateID` when two options in one batch share an `id`
(`_option_list.py:379-382`). The widget it replaced enforced no uniqueness at all — the key
was a plain attribute — so the migration converted "renders an ambiguous list" into "raises
uncaught inside `_fill`".

The exposure is not hypothetical at one call site: `_show_resume_conversations` keys its rows
on `ConversationReference`s that the agent adapters derive from on-disk provider state, and
its `try/except` wraps the catalogue await, not the `_fill` beneath it. A provider reporting
the same conversation twice on one page would have crashed the screen.

These test `_fill`, not that one screen, because `_fill` is where the guard lives and the
choke point every row set passes through. A test written against the resume screen alone
would pass just as well with the guard moved somewhere a future screen could forget it.
"""

from __future__ import annotations

import logging

from test_tui_snapshots import settle
from test_tui_worker_exclusivity import _context, _SlowLauncher
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import RemoteAgentsTui


async def test_a_repeated_key_renders_one_row_instead_of_raising() -> None:
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        # Two rows claiming the same key, which is what a provider reporting one conversation
        # twice produces after `_show_resume_conversations` maps it to `str(item.reference)`.
        app._fill((("same", "First"), ("same", "Second"), ("other", "Third")))
        await pilot.pause()
        choices = app.query_one("#choices", OptionList)
        assert [option.id for option in choices.options] == ["same", "other"]
        assert [str(option.prompt) for option in choices.options] == ["First", "Third"]


async def test_the_first_occurrence_is_the_one_kept() -> None:
    """Which one survives is the behavioural claim, so it is asserted rather than assumed.

    Under the previous widget both rows rendered and selecting either dispatched the same key,
    so keeping the first is the closest match to the outcome the owner used to get.
    """
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        app._fill((("dup", "kept"), ("dup", "dropped")))
        await pilot.pause()
        prompts = [str(option.prompt) for option in app.query_one("#choices", OptionList).options]
        assert prompts == ["kept"]


async def test_the_dropped_row_is_logged_rather_than_silently_swallowed(caplog) -> None:
    """A page that lost a row is a provider bug worth finding; a silent dedup would hide it."""
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        with caplog.at_level(logging.WARNING, logger="remote_agents.adapters.tui.app"):
            app._fill((("dup", "kept"), ("dup", "dropped")))
        await pilot.pause()
        messages = [record.getMessage() for record in caplog.records]
        assert any("dup" in message for message in messages), (
            f"the dropped key was not logged; records were {messages}"
        )


async def test_the_resting_cursor_still_lands_on_a_real_row_after_a_drop() -> None:
    """The highlight is computed against `entries`, so it must be computed after the dedup.

    Against the pre-dedup length, a fill whose last row was a duplicate could rest the cursor
    past the end of what was actually added — and `validate_highlighted` clamps rather than
    rejects, so it would land somewhere unrelated in silence instead of failing.
    """
    app = RemoteAgentsTui(_context(_SlowLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        app._fill((("a", "A"), ("b", "B"), ("a", "A again")), highlight=2)
        await pilot.pause()
        choices = app.query_one("#choices", OptionList)
        assert choices.highlighted is not None
        assert choices.highlighted < len(choices.options)
        assert str(choices.get_option_at_index(choices.highlighted).prompt) == "B"
