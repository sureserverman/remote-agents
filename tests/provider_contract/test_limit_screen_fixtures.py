"""What each agent shows when a usage limit stops it, pinned as fixtures before anything reads it.

The limit-stop watch (Codex, Cursor Agent) and the Claude text hint are built against these, so
a marker this project matches is one somebody saw on a real screen. A screen that could not be
reached is recorded as unmeasured with its reason and the text already on record -- never
invented (`docs/acceptance-2026-09-28-limit-screens.md`).

Each provider directory carries `limit_screen.json`: `_measured`, `_provenance`, `marker` (the
line a watch may match), `composer` (`idle`; `draft` when the unsent prompt is left in the
composer; `dialog`; `null` when unknown) and, when measured, a sibling `limit_screen.txt` holding
the captured pane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"
_PROVIDERS = ("claude", "codex", "cursor")


def _meta(provider: str) -> dict:
    return json.loads((_FIXTURES / provider / "limit_screen.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_every_provider_records_its_limit_screen_or_why_not(provider: str) -> None:
    meta = _meta(provider)

    assert isinstance(meta["_measured"], bool)
    assert meta["_provenance"].strip()
    assert meta["marker"].strip(), "a watch needs a line to match"
    assert meta["composer"] in {"idle", "draft", "dialog", None}


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_a_measured_screen_carries_its_marker(provider: str) -> None:
    meta = _meta(provider)
    screen = _FIXTURES / provider / "limit_screen.txt"
    if not meta["_measured"]:
        assert not screen.exists(), "an unmeasured screen must not ship a capture"
        assert meta["composer"] is None, "an unmeasured screen cannot claim a composer state"
        return

    assert meta["marker"] in screen.read_text(encoding="utf-8")
    assert meta["composer"] is not None
