"""The real folder-trust captures, read by both suites from one copy.

Measured on 2026-09-09 into directories each agent had never been asked about, and recorded
verbatim in `docs/acceptance-2026-09-09-trust-dialogs.md`. Two suites need them and need them
to be the *same* bytes: `tests/provider_contract` cross-checks each vertical's declared
identifier against every agent's capture, and `tests/unit/adapters/tmux` drives the key
arithmetic over them. Two copies of a capture is two dialogs that can drift apart, and the
drift would show up as one suite proving something slightly different from the other.

Lives in `tests/support` -- on the pytest pythonpath for every suite -- for the same reason
`live_probe.py` does: a shared fact with two readers gets one home.
"""

from __future__ import annotations

import pathlib

_CAPTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "trust_dialogs"


def capture(profile: str) -> str:
    """One agent's real pane at the moment it asked (or, for the silent ones, did not)."""
    return (_CAPTURES / f"{profile}.txt").read_text(encoding="utf-8")


def measured_profiles() -> tuple[str, ...]:
    """Every profile a capture was taken from, in a stable order."""
    return tuple(sorted(path.stem for path in _CAPTURES.glob("*.txt")))
