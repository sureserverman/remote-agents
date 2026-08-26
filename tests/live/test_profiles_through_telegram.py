"""Opt-in audit of the real private Telegram lifecycle trace left by the owner journey.

This is the automatable half of BL-002. The other half needs the owner: a lifecycle trace can
be checked *after the fact* even where the tap cannot be driven, and that asymmetry is what
this file exists to exploit.

**What it covers, and what it cannot.** `docs/operator-runbook.md` § *Telegram acceptance
checklist* numbers thirteen
steps of the mobile owner journey. Only some of them leave a durable trace, and the honest
reading is that this file audits those and is silent about the rest:

Auditable here, because they write a durable row:

- **step 2**, launch an agent -- a `ready` event per profile
- **step 3**, rename a session -- a stored label on some session
- **step 5**, stop and close -- the full `graceful_stop_requested` / `pane_exited` /
  `cleanup_confirmed` sequence
- **step 6**, force stop -- a `verified_force_stop` event
- **step 9**, press a button drawn before a restart -- a durable callback row outliving it
- **step 12**, resume prior work -- a session carrying a resume source

Not auditable here, and still owner-witnessed: **steps 1, 4, 7, 8, 10, 11 and 13** -- search,
inline output bounding, paging, project ordering, read-only tmux confirmation, remote-control
toggling and Add Project. All of them are surface behaviour that writes no lifecycle event.

Listing the gap rather than eliding it is the point: an audit that quietly covered six steps
while its name implied thirteen is the failure BL-002's own wording warns about.

**It must not broaden the private control surface**, which is the constraint BL-002 states and
`tests/security/check_surface.py` enforces. Everything here is a read-only SQLite query against
the production database opened `mode=ro`; nothing drives the bot and nothing is sent anywhere.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from pathlib import Path

import pytest

from remote_agents.config import load_config
from remote_agents.domain.profiles import closed_profiles


@pytest.mark.live_acceptance
def test_supported_profiles_have_complete_owner_driven_telegram_lifecycles() -> None:
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
    supported = {str(profile.profile_id) for profile in closed_profiles()}
    graceful = {"ready", "graceful_stop_requested", "pane_exited", "cleanup_confirmed"}
    missing = {
        profile_id: graceful - events
        for profile_id, events in traces.items()
        if profile_id in supported and not graceful <= events
    }
    untraced = supported - traces.keys()

    assert not untraced, f"missing Telegram lifecycle trace for: {sorted(untraced)}"
    assert not missing, f"incomplete Telegram lifecycle trace: {missing}"
    assert any("verified_force_stop" in events for events in traces.values())


@pytest.mark.live_acceptance
def test_the_owner_journey_left_the_durable_trace_its_auditable_steps_imply() -> None:
    """Runbook steps 3, 6, 9 and 12 -- the ones beyond a plain launch-and-stop (BL-002).

    Separate from the per-profile test above because it asks a different question. That one
    asks whether *every supported profile* completed a lifecycle; this asks whether the
    journey as a whole exercised the paths a plain launch-and-stop never touches. A run that
    satisfied the first and failed this one has tested five agents doing the same easy thing.
    """
    database_path = _production_database()

    findings = []
    for description, present in (
        ("step 3 — a renamed session (a stored label)", _any_session_is_labelled(database_path)),
        ("step 6 — a force stop", _any_event_is(database_path, "verified_force_stop")),
        ("step 12 — a resumed conversation", _any_session_was_resumed(database_path)),
        ("step 9 — a callback row outliving a restart", _durable_callbacks_exist(database_path)),
    ):
        if not present:
            findings.append(description)

    assert not findings, "the owner journey left no durable trace of: " + "; ".join(findings)


def _production_database() -> Path:
    """Resolve and gate on the real database, skipping rather than failing when absent."""
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
    return database_path


def _read_only(database_path: Path):
    """Open the production database read-only. Every query in this file goes through here."""
    return sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)


def _scalar(database_path: Path, query: str) -> int:
    connection = _read_only(database_path)
    try:
        return connection.execute(query).fetchone()[0]
    finally:
        connection.close()


def _any_event_is(database_path: Path, event_type: str) -> bool:
    connection = _read_only(database_path)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE event_type = ?", (event_type,)
        ).fetchone()
    finally:
        connection.close()
    return row[0] > 0


def _any_session_is_labelled(database_path: Path) -> bool:
    """A rename is stored on the display identity, which carries five parts when labelled."""
    return (
        _scalar(
            database_path,
            "SELECT COUNT(*) FROM sessions WHERE display_identity LIKE '%·%·%·%·%'",
        )
        > 0
    )


def _any_session_was_resumed(database_path: Path) -> bool:
    return (
        _scalar(database_path, "SELECT COUNT(*) FROM sessions WHERE resume_source_id IS NOT NULL")
        > 0
    )


def _durable_callbacks_exist(database_path: Path) -> bool:
    """Step 9's evidence: a callback row in the store rather than in a dead process's memory.

    The table is what makes a button drawn before a restart still resolve after it. Its
    presence is the auditable half; that the *press* worked is owner-witnessed.

    **A missing table raises rather than answering False.** The first version of this check
    named `callback_state`, and the table is `callback_states` -- so it swallowed its own
    typo and reported "the owner journey left no durable trace of step 9" against a store
    that had the trace all along. An audit that reports a coverage gap when what it actually
    hit was its own wrong table name is worse than one that crashes: the gap is plausible,
    so it gets believed. Schema drift and journey coverage are different findings and this
    now tells them apart.
    """
    connection = _read_only(database_path)
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "callback_states" not in names:
            raise AssertionError(
                "callback_states is absent from the production schema; this audit is checking "
                f"a table that does not exist. Tables present: {sorted(names)}"
            )
        return connection.execute("SELECT COUNT(*) FROM callback_states").fetchone()[0] > 0
    finally:
        connection.close()


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
