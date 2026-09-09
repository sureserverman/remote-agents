"""The argv the console builds to fold its right column away, pinned (DEC-001).

Every tmux verb this project runs is assembled here and nowhere else, so these are the
tests that make that claim checkable for the four verbs the fold needs: read the geometry,
move the split, zoom a named pane, and remember which state the window is in.

Pinned as whole argv tuples rather than by substring. A resize that reaches the wrong
target is not a typo in this project -- the panes hold live agents (DEC-040) -- and a
substring assertion would pass on `-t` naming the window while `-x` moved somebody else.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import (
    PANES_HIDDEN_OPTION,
    console_option_args,
    console_pane_geometry_args,
    console_resize_pane_args,
    console_target,
    console_zoom_pane_args,
    exact_pane_target,
)


def test_the_geometry_read_names_the_console_and_asks_for_three_numbers() -> None:
    """One read answers both questions a slide needs: where it is, and how far it may go."""
    assert console_pane_geometry_args() == (
        "list-panes",
        "-t",
        console_target(),
        "-F",
        "#{pane_id}|#{pane_width}|#{window_width}",
    )


def test_a_resize_names_an_exact_pane_and_a_whole_number_of_columns() -> None:
    assert console_resize_pane_args("%7", 109) == (
        "resize-pane",
        "-t",
        exact_pane_target("%7"),
        "-x",
        "109",
    )


@pytest.mark.parametrize("width", [0, -1, -80])
def test_a_resize_refuses_a_width_no_pane_can_have(width: int) -> None:
    """tmux's floor is one column; a zero or negative width is a caller bug, not a layout."""
    with pytest.raises(ValueError):
        console_resize_pane_args("%7", width)


def test_a_resize_refuses_anything_that_is_not_a_pane_id() -> None:
    """The exact-target rule, restated where a wrong target moves somebody's agent."""
    with pytest.raises(ValueError):
        console_resize_pane_args("ra-console:", 20)


def test_zooming_selects_the_pane_first_because_the_keyboard_follows_the_view() -> None:
    """Zoom hides every other pane; a keyboard left in one of them is in a pane nobody sees.

    Measured on tmux 3.4: `resize-pane -Z` zooms the pane named by `-t`, but the *active*
    pane is unchanged, so zooming the left slot while the sessions pane is active leaves the
    owner typing into a pane that is no longer on screen.
    """
    assert console_zoom_pane_args("%7", zoomed=False, wanted=True) == (
        ("select-pane", "-t", exact_pane_target("%7")),
        ("resize-pane", "-t", exact_pane_target("%7"), "-Z"),
    )


def test_zooming_a_window_already_in_the_wanted_state_issues_nothing() -> None:
    """`resize-pane -Z` is a toggle, so re-sending it in the wanted state undoes it.

    This is the whole reason the verb takes the *current* flag as well as the wanted one: the
    reassert after every exchange (DEC-040's `swap-pane`, which silently unzooms) calls this
    on every path, and a toggle that fired unconditionally would unfold the column each time
    it was already folded.
    """
    assert console_zoom_pane_args("%7", zoomed=True, wanted=True) == ()
    assert console_zoom_pane_args("%7", zoomed=False, wanted=False) == ()


def test_unzooming_does_not_need_to_select_the_pane_first() -> None:
    """Coming back, every pane is visible again, so there is no pane to be lost in."""
    assert console_zoom_pane_args("%7", zoomed=True, wanted=False) == (
        ("resize-pane", "-t", exact_pane_target("%7"), "-Z"),
    )


def test_the_hidden_state_is_a_window_option_on_the_console() -> None:
    """A window option, not tmux's own zoom flag, and that is the load-bearing choice.

    `swap-pane -d` and `split-window` both unzoom the window silently (measured, tmux 3.4),
    so a hidden state stored as `window_zoomed_flag` would pop the column back on every agent
    exchange and every rebuild. The option is ours and survives both.
    """
    assert console_option_args(PANES_HIDDEN_OPTION, "1") == (
        "set-option",
        "-w",
        "-t",
        console_target(),
        PANES_HIDDEN_OPTION,
        "1",
    )


def test_reading_the_hidden_state_is_quiet_about_an_option_never_set() -> None:
    """`-q` because an unset option is the ordinary case: every console before this stage."""
    assert console_option_args(PANES_HIDDEN_OPTION, None) == (
        "show-options",
        "-w",
        "-q",
        "-v",
        "-t",
        console_target(),
        PANES_HIDDEN_OPTION,
    )


def test_the_option_name_is_namespaced_to_this_project() -> None:
    """A tmux user option must start with `@`, and the console window is not ours alone."""
    assert PANES_HIDDEN_OPTION.startswith("@remote_agents")


# --- against a real tmux server ------------------------------------------------------------
#
# The argv assertions above pin what we *send*. These pin what tmux *does* with it, which is
# the half no amount of argv-checking can reach: an option name tmux rejects, a `-w` that
# writes to the session instead of the window, or a `show-options` spelling that answers
# nothing would all pass every test above.


async def test_the_hidden_option_round_trips_on_a_real_console_window(tmp_path) -> None:
    """Write it, read it back, and confirm it is a *window* option rather than a pane one."""
    import uuid

    from remote_agents.adapters.tmux.gateway import TmuxGateway
    from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner

    socket = f"remote-agents-test-codec-{uuid.uuid4().hex[:8]}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner)
    try:
        await runner.run(
            "tmux", "-L", socket, "new-session", "-d", "-s", console_target().rstrip(":"), "-x", "80",
            "-y", "24", "sleep 30",
        )

        assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == "", (
            "an option nobody has written must read empty, not raise: that is every console "
            "built before this stage"
        )

        await gateway.write_console_option(PANES_HIDDEN_OPTION, "1")
        assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == "1"

        # A *window* option: readable through `-w`, and absent from the pane's own scope.
        pane_scoped = await runner.run(
            "tmux", "-L", socket, "show-options", "-p", "-q", "-v", PANES_HIDDEN_OPTION
        )
        assert pane_scoped.strip() == "", (
            "the flag landed on the pane rather than the window; an exchange would carry it "
            "into an agent's window and leave the console's own state behind"
        )

        await gateway.write_console_option(PANES_HIDDEN_OPTION, "")
        assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == ""
    finally:
        await runner.run("tmux", "-L", socket, "kill-server")


async def test_the_geometry_read_answers_a_real_window_with_its_width(tmp_path) -> None:
    """The parse is pinned above; this is that the format string is one tmux accepts."""
    import uuid

    from remote_agents.adapters.tmux.gateway import TmuxGateway
    from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner

    socket = f"remote-agents-test-geom-{uuid.uuid4().hex[:8]}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner)
    try:
        await runner.run(
            "tmux", "-L", socket, "new-session", "-d", "-s", console_target().rstrip(":"), "-x", "183",
            "-y", "44", "sleep 30",
        )
        await runner.run("tmux", "-L", socket, "split-window", "-h", "-t", console_target(), "sleep 30")

        geometry = await gateway.console_pane_geometry()

        panes = [entry for entry in geometry if entry[0]]
        (window,) = [entry for entry in geometry if not entry[0]]
        assert len(panes) == 2, geometry
        assert window[1] == 183, f"the window width did not come back: {geometry}"
        # The two panes and the divider account for the window: this is the arithmetic the
        # slide depends on, so it is asserted against a real split rather than assumed.
        assert sum(width for _id, width in panes) + 1 == 183, geometry
    finally:
        await runner.run("tmux", "-L", socket, "kill-server")
