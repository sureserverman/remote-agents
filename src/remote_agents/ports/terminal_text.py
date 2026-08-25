"""Technology-neutral safety transformation for bounded terminal text."""

from __future__ import annotations

import re

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

#: The terminal default both surfaces are read in.
_TAB_WIDTH = 8


def sanitize_terminal_text(
    raw: bytes,
    *,
    max_lines: int,
    max_bytes: int,
    redactions: tuple[str, ...] = (),
) -> str:
    """Return bounded UTF-8 text with unsafe controls and configured secrets removed."""
    if max_lines < 1 or max_bytes < 1:
        raise ValueError("capture limits must be positive")
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    text = _ANSI_ESCAPE.sub("", text)
    # Tabs become spaces *before* the control filter, rather than being deleted by it. The
    # filter keeps only `\n` and anything at or above `" "`, and `\t` is 0x09 — so a tab was
    # dropped with nothing in its place and tab-separated agent output arrived as joined
    # words: `col1\tcol2` rendered as `col1col2`. Neither surface could tell that had
    # happened, because the damage is indistinguishable from output that never had columns.
    #
    # Expanded rather than exempted. Passing `\t` through the filter would put a real control
    # character into a string this function exists to promise has none, and both consumers
    # render it into a terminal. A fixed eight-column stop is the ordinary terminal default;
    # `expandtabs` computes it per line, which is what keeps a column that follows a wide
    # prefix from collapsing.
    text = text.expandtabs(_TAB_WIDTH)
    text = "".join(character for character in text if character == "\n" or character >= " ")
    for pattern in redactions:
        if pattern:
            text = text.replace(pattern, "[REDACTED]")
    return "\n".join(text.splitlines()[:max_lines]).strip()


def encodable_text(text: str) -> str:
    """Replace every code point no encoder will carry, so downstream text handling is total.

    Here, beside `sanitize_terminal_text`, because it answers the same kind of question about a
    different input. That one takes bytes this project decoded itself and is safe by
    construction; this one takes a `str` that arrived **already decoded by somebody else**, and
    such a string can hold a lone surrogate — legal in memory, refused by every encoder.

    Two producers do exactly that today, and neither is exotic:

    - `json.loads` turns a `\\udXXX` escape in a spooled hook payload back into a lone
      surrogate, and the spool tolerates a foreign writer by design;
    - `os.listdir` decodes an undecodable filename with `surrogateescape`, so a project
      directory whose name is not valid UTF-8 carries one into the project list.

    Both reached a UTF-16 budget calculation and raised `UnicodeEncodeError` out of the render.
    U+FFFD is the replacement, matching what `sanitize_terminal_text`'s decode already produces,
    so one marker means "this was not text" wherever the reader meets it.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "".join(character if _encodable(character) else "�" for character in text)
    return text


def _encodable(character: str) -> bool:
    try:
        character.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


#: How much of a version probe's output is even looked at, before any of it is processed.
#:
#: The cap is on the *input*, not just the result, and that is the correction it exists for.
#: Bounding only the return value left every intermediate step — `splitlines`, the strip, the
#: filtered join — running over whatever a foreign program chose to print, which a program can
#: make gigabytes of inside the five-second timeout its runner allows. A version banner that
#: does not fit in four kilobytes is not a version banner.
_VERSION_PROBE_BUDGET = 4096

#: What survives into a report. Code points, not display columns: a line of wide characters
#: still renders wider than this, which is cosmetic in a diagnostic and not worth a second
#: measure.
_VERSION_LINE_LENGTH = 160


def probe_version_line(value: str) -> str | None:
    """Reduce what an executable printed to one printable, bounded line, or to nothing.

    Two callers ask this question about two different populations — the curated agent
    executables (`adapters/tmux/profiles.probe_profiles`) and onboarding's system dependencies
    (`application/dependencies.probe_dependencies`) — and it is one question, not two: how much
    of a program this project did not write is safe to print in a report. It lived in both
    places, byte for byte, until a review pointed out that a change to the bound or to the
    filter had a 50% chance of reaching the call site that needed it (DEC-043 — a shared rule
    is asked, not restated).

    It lives in `ports/` because that is the only layer both callers may import: `application/`
    may not import an adapter, and `adapters/tmux` is not a driver adapter and so may not
    import `application/` either (ARCH-02, `tests/architecture/check_imports.py`).

    `None` rather than a raise, so neither caller has to agree with the other about which
    exception type means "nothing usable". Both already have a well-defined answer for a probe
    that did not answer.

    The printable filter is doing more than it looks like. `str.isprintable()` is False for
    every `Cc` and `Cf` code point, which covers `ESC` — so an ANSI sequence degrades to its
    harmless literal tail rather than reaching a terminal — and also `U+202E` RIGHT-TO-LEFT
    OVERRIDE, `U+200B` ZERO WIDTH SPACE and `U+00AD` SOFT HYPHEN, which are the ones a reader
    would not think to check for.
    """
    for candidate in value[:_VERSION_PROBE_BUDGET].splitlines():
        line = candidate.strip()
        if not line:
            continue
        printable = "".join(character for character in line if character.isprintable())
        return printable[:_VERSION_LINE_LENGTH] or None
    return None
