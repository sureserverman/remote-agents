"""Opt-in audit of each qualified profile's real private Telegram lifecycle trace."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from pathlib import Path

import pytest

from remote_agents.config import load_config
from remote_agents.domain.profiles import qualified_profiles


@pytest.mark.live_acceptance
def test_qualified_profiles_have_complete_owner_driven_telegram_lifecycles() -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    config_path = Path(
        os.environ.get("REMOTE_AGENTS_CONFIG", "~/.config/remote-agents/config.toml")
    ).expanduser()
    if not config_path.is_file():
        pytest.skip("BLOCKED: production config is unavailable")
    database_path = load_config(config_path).database_path
    if not database_path.is_file():
        pytest.skip("BLOCKED: production session database is unavailable")

    traces = _traces(database_path)
    qualified = {str(profile.profile_id) for profile in qualified_profiles()}
    graceful = {"ready", "graceful_stop_requested", "pane_exited", "cleanup_confirmed"}
    missing = {
        profile_id: graceful - events
        for profile_id, events in traces.items()
        if profile_id in qualified and not graceful <= events
    }
    untraced = qualified - traces.keys()

    assert not untraced, f"missing Telegram lifecycle trace for: {sorted(untraced)}"
    assert not missing, f"incomplete Telegram lifecycle trace: {missing}"
    assert any("verified_force_stop" in events for events in traces.values())


def _traces(database_path: Path) -> dict[str, set[str]]:
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT sessions.profile_id, session_events.event_type
            FROM sessions
            JOIN session_events USING (session_id)
            """
        ).fetchall()
    finally:
        connection.close()
    traces: dict[str, set[str]] = defaultdict(set)
    for profile_id, event_type in rows:
        traces[profile_id].add(event_type)
    return dict(traces)
