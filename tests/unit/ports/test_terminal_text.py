"""The shared safety transformation, tested where it lives rather than through two surfaces.

`sanitize_terminal_text` had no test file of its own until a gate evaluator drove real agent
output through the local pane and found tab-separated columns arriving as joined words. Both
adapters call this function, so a defect here is a defect on both surfaces at once — and
neither surface's tests could have located it, because from inside either one the damage is
indistinguishable from output that never had columns.
"""

from __future__ import annotations

import pytest

from remote_agents.ports.terminal_text import probe_version_line, sanitize_terminal_text

_BOUNDS = {"max_lines": 100, "max_bytes": 64 * 1024}


def test_tabs_become_spaces_rather_than_vanishing() -> None:
    """`\\t` is 0x09, below the control filter's floor, so it used to be deleted outright."""
    result = sanitize_terminal_text(b"col1\tcol2\tcol3\nname\tvalue", **_BOUNDS)

    assert result == "col1    col2    col3\nname    value"


def test_a_tab_stop_is_computed_per_line_not_padded_blindly() -> None:
    """Expansion, not substitution: a column after a long prefix still lines up."""
    result = sanitize_terminal_text(b"a\tX\nlonger-prefix\tX", **_BOUNDS)
    first, second = result.splitlines()

    assert first.index("X") == 8
    assert second.index("X") == 16


def test_no_control_character_survives_the_tab_expansion() -> None:
    """The expansion runs before the filter, so it cannot smuggle one past it.

    Stated as a property rather than a case, because this is the function's whole promise:
    both adapters render its output straight into a terminal.
    """
    raw = b"a\tb\x07c\x1b[31md\x00e\vf\rg\n\tindented"

    result = sanitize_terminal_text(raw, **_BOUNDS)

    assert all(character == "\n" or character >= " " for character in result), repr(result)
    assert "\t" not in result


def test_ansi_sequences_are_still_stripped() -> None:
    assert sanitize_terminal_text(b"\x1b[31mred\x1b[0m text", **_BOUNDS) == "red text"


def test_redactions_are_still_applied_after_expansion() -> None:
    result = sanitize_terminal_text(b"token\thunter2", redactions=("hunter2",), **_BOUNDS)

    assert "hunter2" not in result
    assert "[REDACTED]" in result


def test_the_line_bound_is_still_enforced() -> None:
    raw = "\n".join(f"line {index}" for index in range(50)).encode()

    result = sanitize_terminal_text(raw, max_lines=10, max_bytes=64 * 1024)

    assert len(result.splitlines()) == 10


def test_undecodable_bytes_become_the_replacement_character_rather_than_raising() -> None:
    """Rehomed from `tests/unit/adapters/tmux/test_capture.py`, deleted with `sanitize_capture`.

    It was the **only** test in the suite covering this, and the decode is this module's, not
    the tmux adapter's — a pane holding a stray `\xff` must come back as bounded text rather
    than raising `UnicodeDecodeError` at whichever surface asked for it. Nothing else asserts
    it: `render_capture` refuses NUL before decoding, but invalid UTF-8 is not NUL and reaches
    the sanitizer intact.
    """
    result = sanitize_terminal_text(b"ok\xff\n" + b"x" * 100, max_lines=2, max_bytes=8)

    assert result == "ok\ufffd\nxxxx"


def test_the_byte_bound_slices_before_decoding() -> None:
    result = sanitize_terminal_text(b"x" * 500, max_lines=10, max_bytes=100)

    assert result == "x" * 100


@pytest.mark.parametrize("bound", [{"max_lines": 0}, {"max_bytes": 0}])
def test_a_non_positive_bound_is_refused(bound: dict[str, int]) -> None:
    limits = {**_BOUNDS, **bound}

    with pytest.raises(ValueError):
        sanitize_terminal_text(b"anything", **limits)


class TestProbeVersionLine:
    """The version-line reducer, tested at its new shared home rather than through two probes.

    It was a private copy in `adapters/tmux/profiles.py` and another in
    `application/dependencies.py`, and neither had a test of its own — so the two could drift
    and the only thing that would notice was whichever caller happened to be exercised.
    """

    def test_the_first_non_empty_line_is_what_a_report_gets(self) -> None:
        assert probe_version_line("tmux 3.4\ncopyright\n") == "tmux 3.4"
        assert probe_version_line("\n\n  git version 2.43.0  \n") == "git version 2.43.0"

    def test_output_with_nothing_usable_in_it_is_nothing_rather_than_an_empty_string(self) -> None:
        """`None`, not `""`, and that is a deliberate change from what the adapter copy did.

        The old private version returned an empty string for a first line that was entirely
        non-printable, so `probe_profiles` reported an available profile carrying `version=""`
        and no note — an "AVAILABLE, nothing to say" row that had in fact failed to read a
        version. Both callers now have one answer for "the probe did not answer".
        """
        assert probe_version_line("") is None
        assert probe_version_line("\n \n\t\n") is None
        assert probe_version_line("\x07\x07\n") is None

    def test_ansi_and_the_invisible_code_points_do_not_survive(self) -> None:
        """`str.isprintable()` is False for `Cc` and `Cf`, which is wider than it looks."""
        assert probe_version_line("\x1b[31mtmux 3.4\x1b[0m") == "[31mtmux 3.4[0m"
        assert probe_version_line("tmux‮3.4") == "tmux3.4"
        assert probe_version_line("tmux​3.4") == "tmux3.4"

    def test_the_returned_line_is_bounded(self) -> None:
        assert len(probe_version_line("v" * 500)) == 160

    def test_the_input_is_bounded_too_and_a_late_version_is_the_accepted_cost(self) -> None:
        """The bound is on what is read, not only on what is returned.

        Bounding the result alone left `splitlines`, the strip and the filtered join all running
        over whatever a foreign program chose to print, and a program can print gigabytes inside
        the five seconds its runner allows. The cost, stated rather than hidden: a version banner
        preceded by four kilobytes of blank lines is now reported as no version at all. A version
        banner that does not fit in four kilobytes is not a version banner.
        """
        assert probe_version_line("\n" * 5000 + "tmux 3.4") is None
        assert probe_version_line("\n" * 100 + "tmux 3.4") == "tmux 3.4"
