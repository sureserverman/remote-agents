"""A `limit_screen` is a capability like any other: each declared one reads its own agent's stop.

Driven through `drive_or_skip`. The evidence is the fixture Task 1.1 pinned for each agent
(`fixtures/<provider>/limit_screen.json`, `docs/acceptance-2026-09-28-limit-screens.md`): a
declared limit screen must match its own agent's marker, and its hint must read that marker as a
limit stop. A measured capture is also swept whole, so a marker written for the wrong line fails.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from kit import drive_or_skip

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_DIRECTORY = {"claude": "claude", "codex": "codex", "cursor-agent": "cursor"}


def _meta(profile: str) -> tuple[dict, Path]:
    directory = _FIXTURES / _DIRECTORY[profile]
    return json.loads((directory / "limit_screen.json").read_text(encoding="utf-8")), directory


def test_a_declared_limit_screen_matches_and_reads_its_own_stop(descriptor) -> None:
    screen = drive_or_skip(descriptor, "limit_screen")
    meta, directory = _meta(str(descriptor.profile_id))
    text = (
        (directory / "limit_screen.txt").read_text(encoding="utf-8")
        if meta["_measured"]
        else meta["marker"]
    )

    assert any(re.search(marker, text, re.MULTILINE) for marker in screen.markers), text
    assert screen.hint(text, datetime.now(UTC)) is not None
