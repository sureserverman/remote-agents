"""The launch environment a managed pane gets, and the one field that was missing from it."""

from __future__ import annotations

from remote_agents.composition.tui import _curated_environment


def test_a_composer_with_no_terminal_still_hands_the_agent_a_colour_capable_term() -> None:
    """The bug this exists for: the bot is a systemd service, and a service has no TERM.

    `execvpe` replaces the environment rather than adding to it, so an absent TERM reached the
    agent as an absent TERM — and every agent CLI's colour detection answers "no capability" to
    that. Sessions launched from the bot rendered white; the identical session launched from the
    TUI, which always has a terminal, rendered in colour.
    """
    curated = _curated_environment({"HOME": "/home/owner", "PATH": "/usr/bin"})

    assert curated["TERM"] == "xterm-256color"


def test_a_composer_with_a_terminal_keeps_the_one_it_has() -> None:
    """The default is a floor for the case with no answer, never an override of a real one."""
    curated = _curated_environment({"HOME": "/home/owner", "TERM": "screen-256color"})

    assert curated["TERM"] == "screen-256color"


def test_a_terminal_that_announces_it_knows_nothing_is_treated_as_absent() -> None:
    """`dumb` is what a process reports when it has no terminal information, which is this case."""
    curated = _curated_environment({"TERM": "dumb"})

    assert curated["TERM"] == "xterm-256color"


def test_colorterm_is_carried_when_the_composer_has_one() -> None:
    curated = _curated_environment({"TERM": "xterm-256color", "COLORTERM": "truecolor"})

    assert curated["COLORTERM"] == "truecolor"


def test_colorterm_is_never_invented() -> None:
    """A truecolour claim is the terminal's to make; a service with no terminal cannot make it."""
    assert "COLORTERM" not in _curated_environment({"HOME": "/home/owner"})


def test_nothing_outside_the_allowlist_reaches_the_agent() -> None:
    """This mapping is the whole environment the pane gets, so a leak here is a leak into it."""
    curated = _curated_environment(
        {"HOME": "/home/owner", "AWS_SECRET_ACCESS_KEY": "not-this", "TERM": "xterm"}
    )

    assert set(curated) == {"HOME", "TERM"}
