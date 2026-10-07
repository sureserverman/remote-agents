"""Every tmux server a test names is one `tests/conftest.py` knows how to clean up.

tmux 3.4 never removes its socket file -- not on `kill-server`, not when the last session
ends (measured 2026-10-07) -- so a test's dedicated server leaves a file behind unless
something unlinks it. `tests/conftest.py` does, for every socket named `remote-agents-test-*`:
dead ones at session start, this run's at session finish. A socket named any other way escapes
both. On 2026-10-07, 5,206 dead files had built up in `/tmp/tmux-<uid>/` from three test
modules that named theirs `remote-agents-bar-in-`, `-fkey-` and `-probe-*`, after 108 `fkey`
sockets had already been cleared by hand on 2026-09-17 and the leak left in place.

So the rule is a property of the set of names, swept over the source: a generated socket name
(an f-string beginning `remote-agents-`) must begin with the prefix the cleanup reaps.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS = Path("tests")

#: The prefix `tests/conftest.py` reaps, restated so this module needs no conftest import; the
#: second test pins the two equal.
PREFIX = "remote-agents-test-"

#: An f-string that builds a `remote-agents-…` name with a generated part.
_GENERATED = re.compile(r"""f["'](remote-agents-[^"'{]*)\{""")


def test_every_generated_tmux_socket_name_carries_the_reaped_prefix() -> None:
    offenders = [
        f"{path}:{number}: {match.group(1)}"
        for path in sorted(_TESTS.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in _GENERATED.finditer(line)
        if not match.group(1).startswith(PREFIX)
    ]
    assert not offenders, "socket names the cleanup never reaps:\n  " + "\n  ".join(offenders)


def test_the_prefix_is_the_one_the_cleanup_reaps() -> None:
    conftest = (_TESTS / "conftest.py").read_text(encoding="utf-8")
    assert f'_TEST_SOCKET_PREFIX = "{PREFIX}"' in conftest
