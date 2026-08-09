"""The shared safety transformation, tested where it lives rather than through two surfaces.

`sanitize_terminal_text` had no test file of its own until a gate evaluator drove real agent
output through the local pane and found tab-separated columns arriving as joined words. Both
adapters call this function, so a defect here is a defect on both surfaces at once — and
neither surface's tests could have located it, because from inside either one the damage is
indistinguishable from output that never had columns.
"""

from __future__ import annotations

import pytest

from remote_agents.ports.terminal_text import sanitize_terminal_text

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


def test_the_byte_bound_slices_before_decoding() -> None:
    result = sanitize_terminal_text(b"x" * 500, max_lines=10, max_bytes=100)

    assert result == "x" * 100


@pytest.mark.parametrize("bound", [{"max_lines": 0}, {"max_bytes": 0}])
def test_a_non_positive_bound_is_refused(bound: dict[str, int]) -> None:
    limits = {**_BOUNDS, **bound}

    with pytest.raises(ValueError):
        sanitize_terminal_text(b"anything", **limits)
