"""Opt-in proof that a real claude session spools activity, and only a managed one does.

Everything else about this feature is checked against fixtures and fake payloads. This is the
one place a real `claude` runs with a real hook installed, because the two claims that matter
most cannot be established any other way: that the hook fires at all against the agent as
shipped, and that a session the service did not start stays silent. The second is a negative
about a process the service never sees, so it has to be produced rather than swept for.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required, as it is for every file here. The hook is
installed into a settings file made for the test and passed with `--settings`, and it writes
to a spool made for the test and passed with `--activity-dir`; neither the operator's
`~/.claude/settings.json` nor their real spool is read or written.

`HOME` is deliberately *not* isolated. An earlier version pointed it at a temporary directory
for tidiness, and the drill immediately reported that a managed session produced `SessionEnd`
and no `Stop` -- because a `claude` with no credentials exits at the login prompt rather than
taking a turn. That is the isolation, not the agent, and a drill whose whole purpose is to
observe a real turn has to let the agent actually take one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.agents.hook_install import install_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.domain.models import SessionId
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

_TURN = "Reply with exactly the word: spooled"


def _requirements(tmp_path: Path) -> tuple[Path, Path]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    if shutil.which("claude") is None:
        pytest.skip("BLOCKED: executable_missing")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "sonnet"}, indent=2) + "\n", encoding="utf-8")
    spool = tmp_path / "activity"
    install_agent_hooks(settings, executable=Path(sys.executable), activity_directory=spool)
    return settings, spool


def _run_claude(settings: Path, workspace: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        ["claude", "-p", _TURN, "--settings", str(settings)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=workspace,
        env={**os.environ, **environment},
    )
    if "Please run /login" in f"{completed.stdout}{completed.stderr}":
        pytest.skip("BLOCKED: claude is not logged in")
    return completed.stdout


@pytest.mark.live_profile
def test_a_managed_claude_session_spools_its_own_stop(tmp_path: Path) -> None:
    settings, spool = _requirements(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = SessionId.new()

    _run_claude(settings, workspace, {SESSION_ID_VARIABLE: str(session_id)})

    activities = drain_activity(spool)
    assert activities, "a managed session's Stop hook spooled nothing"
    assert {activity.session_id for activity in activities} == {str(session_id)}
    assert ActivityKind.COMPLETED in {activity.kind for activity in activities}


@pytest.mark.live_profile
def test_a_session_this_service_did_not_start_spools_nothing(tmp_path: Path) -> None:
    """The guard, against the real agent rather than against a fake payload."""
    settings, spool = _requirements(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _run_claude(settings, workspace, {SESSION_ID_VARIABLE: ""})

    assert drain_activity(spool) == ()
    assert not spool.exists() or list(spool.iterdir()) == []


_DISCRIMINATORS = {
    "StopFailure": "error",
    "Notification": "notification_type",
    "SessionEnd": "reason",
}
"""The field each event discriminates on, as this project believes the agent spells them.

The belief `_DISCRIMINATING_FIELDS` is built on, stated once so a single test can check it
against reality rather than against another fixture.
"""


def _installed_bundle() -> Path | None:
    """The Claude Code bundle this host would actually run, or None if it cannot be found."""
    executable = shutil.which("claude")
    if executable is None:
        return None
    versions = Path.home() / ".local" / "share" / "claude" / "versions"
    try:
        current = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout.split()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None
    bundle = versions / current
    return bundle if bundle.is_file() else None


@pytest.mark.live_profile
def test_the_hook_payload_field_names_match_the_installed_agent() -> None:
    """Compare this project's assumption against the agent, not against its own fixtures.

    This is the test whose absence let `limit_reached` ship dead. The spool's unit test
    fixtured `error_type` and the classifier's unit test wrote `reason="rate_limit"` straight
    into a spool record, so each half was verified against the other half's assumption and the
    pair agreed perfectly about a field the agent has never sent. `error_type` appears nowhere
    in the shipped bundle. A managed session hitting a rate limit spooled a record the drain
    then dropped as uninterpretable -- no message, no error, no way to notice.

    Static, and deliberately so: provoking a real `StopFailure` means exhausting a real rate
    limit. Reading how the shipped bundle *constructs* the payload is the strongest claim
    available without that, and it is strictly stronger than another fixture. It skips rather
    than fails when the bundle cannot be located or its shape is unrecognisable, because an
    upstream repackaging is not this project's defect -- but a name that is present and
    *different* is, and that is the case this fails on.
    """
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    bundle = _installed_bundle()
    if bundle is None:
        pytest.skip("BLOCKED: the installed claude bundle could not be located")
    source = bundle.read_text(encoding="utf-8", errors="replace")

    unrecognised = [
        event for event in _DISCRIMINATORS if f'hook_event_name:"{event}"' not in source
    ]
    if unrecognised:
        pytest.skip(f"BLOCKED: payload construction not recognisable for {unrecognised}")

    wrong = {}
    for event, expected in _DISCRIMINATORS.items():
        start = source.index(f'hook_event_name:"{event}"')
        # The payload object literal, up to its close -- long enough to carry every field the
        # event sets, short enough not to run into the next statement.
        window = source[start : start + 240]
        if f"{expected}:" not in window:
            wrong[event] = window[: window.find("}") if "}" in window else 200]

    assert not wrong, (
        "the installed agent does not spell these discriminating fields the way "
        f"activity_spool._DISCRIMINATING_FIELDS expects: {wrong}"
    )
