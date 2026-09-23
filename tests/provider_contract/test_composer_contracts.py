"""A `composer` is a capability like any other: each declared one reads its own agent's screens.

Driven through `drive_or_skip`, the kit's rule, never an inline `if x is None`. The captures are
the ones `docs/acceptance-2026-09-22-composer-states.md` records, under `tests/fixtures/panes/`;
`tests/unit/adapters/tmux/test_composer.py` sweeps every one of them, and this file holds the
per-provider contract: a declared composer must find its own idle screen idle and its own busy
screen busy, and must not read any other agent's idle screen as idle.
"""

from __future__ import annotations

from pathlib import Path

from kit import drive_or_skip

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.tmux.composer import PaneState, classify

_PANES = Path(__file__).resolve().parents[1] / "fixtures" / "panes"
_DIRECTORY = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "cursor-agent": "cursor",
}


def _screen(profile: str, name: str) -> str:
    return (_PANES / _DIRECTORY[profile] / f"{name}.txt").read_text(encoding="utf-8")


def test_a_declared_composer_reads_its_own_idle_and_busy_screens(descriptor) -> None:
    drive_or_skip(descriptor, "composer")
    profile = str(descriptor.profile_id)

    assert classify(_screen(profile, "idle"), descriptor) is PaneState.IDLE
    assert classify(_screen(profile, "busy"), descriptor) is PaneState.BUSY


def test_a_declared_composer_reads_no_other_agent_s_idle_screen_as_idle(descriptor) -> None:
    drive_or_skip(descriptor, "composer")
    profile = str(descriptor.profile_id)

    for other in (str(one.profile_id) for one in provider_descriptors()):
        if other != profile and other in _DIRECTORY:
            assert classify(_screen(other, "idle"), descriptor) is not PaneState.IDLE, other
