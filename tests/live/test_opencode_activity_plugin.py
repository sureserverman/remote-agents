"""Opt-in proof that a real OpenCode turn loads this project's own generated plugin.

Everything the drill writes is disposable. The plugin, the `opencode.json` naming it and every
byte of OpenCode's writable state live under this test's temporary directory, reached through a
throwaway ``XDG_CONFIG_HOME`` and ``XDG_DATA_HOME``; the owner's real configuration, their real
``opencode.db`` and the service's production spool are neither read nor written.

**That last part is a correction of the measurement drill this vertical was built on.** Stage 4
(`docs/acceptance-2026-09-06-opencode-activity.md`) relocated only ``XDG_CONFIG_HOME``,
deliberately, so the owner's credentials would work — and the consequence, which the document
records rather than hides, is that both completing runs persisted as live session rows in the
owner's real OpenCode database. Symlinking `auth.json` into a disposable data home, the way
`test_codex_activity_hooks.py` symlinks Codex's, keeps the entitlement and drops the pollution.
The credential is never opened, copied, logged or modified here; only OpenCode reads it.

What this drill proves that no other test can: `tests/provider_contract` drives the generated
file under a bare `node` harness this project wrote, so it proves the *plugin* behaves. Only a
real `opencode` proves the thing nobody can assert from the outside — that OpenCode loads a
`plugin` entry this installer wrote and delivers `session.idle` to it.

**It has not yet proved that.** First run, 2026-09-06: every attempt was blocked upstream of the
plugin by an unavailable model — `AI_APICallError: The usage limit has been reached` on the
configured provider, the local ollama holding no models, and no third route. A clean A/B on that
host, two disposable config homes identical but for the `plugin` entry, reached the *same* point
in OpenCode's startup either way, so the entry demonstrably changes nothing about what could be
reached — but "changes nothing" is not the claim this file exists to make. Until it runs green
somewhere, the load route rests on Stage 4's measurement with a throwaway plugin, and that
should be read as evidence for the mechanism rather than for this artifact.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import install_agent_hooks, remove_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.domain.models import SessionId
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

_TURN = "Reply with exactly the word: spooled"

#: Markers of an unavailable model, matched against OpenCode's **log file** and not its output.
#:
#: The log is where they appear, and that is the whole point of this table. `opencode run` prints
#: nothing at all when a provider refuses: it retries the stream with backoff and never exits, so
#: a drill watching stdout sees a silent process and calls it a hang. That is exactly what Stage 4
#: recorded -- "three runs hung ... Recorded because it bears on how a live drill should be
#: driven; not understood, and not claimed to be" -- and it is understood now. The first attempt
#: at this drill reproduced it: 300 seconds of silence, and one line in
#: `$XDG_DATA_HOME/opencode/log/opencode.log` reading
#: `AI_APICallError: The usage limit has been reached`.
_BLOCKED_LOG = {
    "quota or billing": ("usage limit", "rate limit", "billing", "credits", "quota exceeded"),
    "authentication": ("unauthorized", "invalid api key", "not authenticated"),
    "network": ("econnrefused", "connection refused", "enotfound"),
}

#: Long enough for a real turn, short enough that an unavailable model is reported rather than
#: waited out. A provider that is going to answer answers in seconds; one that is not never will.
_TURN_TIMEOUT_SECONDS = 120


def _requirements(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    if shutil.which("opencode") is None:
        pytest.skip("BLOCKED: executable_missing")

    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    (config_home / "opencode").mkdir(parents=True)
    (data_home / "opencode").mkdir(parents=True)

    source_auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if not source_auth.is_file():
        pytest.skip("BLOCKED: OpenCode is not logged in")
    os.symlink(source_auth, data_home / "opencode" / "auth.json")

    settings = config_home / "opencode" / "opencode.json"
    settings.write_text(json.dumps({"$schema": "https://opencode.ai/config.json"}) + "\n", "utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spool = tmp_path / "activity"

    install_agent_hooks(
        settings,
        executable=Path(sys.executable),
        activity_directory=spool,
        provider="opencode",
    )
    plugin = settings.parent / "remote-agents" / "activity-plugin.mjs"
    assert plugin.is_file(), "the drill must exercise a plugin the real installer wrote"
    assert json.loads(settings.read_text(encoding="utf-8"))["plugin"] == [plugin.as_uri()]

    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
    }
    # This pytest process can itself be a managed agent turn. Its identity belongs to the parent,
    # not to the disposable child, so inheritance is an explicit opt-in rather than an accidental
    # second managed session -- the same reasoning `test_codex_activity_hooks.py` records.
    environment.pop(SESSION_ID_VARIABLE, None)
    return workspace, spool, environment


def _blocked_reason(environment: dict[str, str]) -> str | None:
    """Classify an unavailable model from OpenCode's own log, or `None` if it says nothing."""
    log = Path(environment["XDG_DATA_HOME"]) / "opencode" / "log" / "opencode.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return None
    return next(
        (name for name, markers in _BLOCKED_LOG.items() if any(m in text for m in markers)),
        None,
    )


def _run_opencode(workspace: Path, environment: dict[str, str], turn: str = _TURN) -> None:
    try:
        completed = subprocess.run(
            ["opencode", "run", turn],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TURN_TIMEOUT_SECONDS,
            cwd=workspace,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        # A silent process is the *symptom* of a refused provider, never the diagnosis. Ask the
        # log before calling it a hang -- and if the log is silent too, say that the drill could
        # not start rather than that the plugin failed, because nothing here has been tested yet.
        reason = _blocked_reason(environment)
        pytest.skip(
            f"BLOCKED: OpenCode {reason} is unavailable for the live drill"
            if reason
            else "BLOCKED: `opencode run` did not finish and its log names no cause; the model "
            "path is unavailable, which is upstream of anything this drill tests"
        )
    reason = _blocked_reason(environment)
    if reason is not None:
        pytest.skip(f"BLOCKED: OpenCode {reason} is unavailable for the live drill")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.live_profile
def test_a_managed_opencode_turn_spools_its_own_completion(tmp_path: Path) -> None:
    """The one link no harness of ours can prove: OpenCode loading the file we installed."""
    workspace, spool, environment = _requirements(tmp_path)
    session_id = SessionId.new()

    _run_opencode(workspace, {**environment, SESSION_ID_VARIABLE: str(session_id)})

    activities = drain_activity(spool)
    assert activities, "a managed OpenCode turn spooled nothing; the plugin did not run"
    completions = [one for one in activities if one.kind is ActivityKind.COMPLETED]
    assert completions, f"no completion among {[one.kind for one in activities]}"
    for activity in activities:
        assert activity.session_id == str(session_id)
    # `detail is None` is the assertion, not an omission. `session.idle` carries one field and
    # that field is OpenCode's own session id, so a completion from this source can never carry
    # the agent's words -- a property of the event, not a parser waiting to be widened. A detail
    # arriving here would mean the spool had started reading something nobody licensed.
    assert all(one.detail is None for one in activities)


@pytest.mark.live_profile
def test_an_unmanaged_opencode_turn_spools_nothing(tmp_path: Path) -> None:
    """The plugin loads in every session on the host; only a managed one may write."""
    workspace, spool, environment = _requirements(tmp_path)

    _run_opencode(workspace, environment)

    assert drain_activity(spool) == ()
    assert not spool.exists() or list(spool.iterdir()) == []


@pytest.mark.live_profile
def test_the_drill_leaves_the_configuration_as_it_found_it(tmp_path: Path) -> None:
    """Reversibility asserted against a config a real OpenCode has since loaded and written."""
    workspace, _spool, environment = _requirements(tmp_path)
    settings = Path(environment["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
    plugin = settings.parent / "remote-agents" / "activity-plugin.mjs"

    _run_opencode(workspace, environment)
    remove_agent_hooks(settings, provider="opencode")

    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "$schema": "https://opencode.ai/config.json"
    }
    assert not plugin.exists()
