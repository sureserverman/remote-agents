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
import time
from pathlib import Path

import pytest
from agent_panes import (
    CLAUDE_OPENING,
    CLAUDE_READY,
    CODEX_OPENING,
    CODEX_READY,
    Interstitial,
    open_to_composer,
)

from remote_agents.adapters.agents.registry import install_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.domain.models import SessionId
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity
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
    pair agreed perfectly about a field the agent has never sent. (`error_type` is a real
    string in the bundle -- 58 times, all telemetry -- but never a hook payload field, which
    is why searching for the name alone would have been reassuring and wrong. This test
    searches where the payload is *built*.) A managed session hitting a rate limit spooled a
    record the drain then dropped as uninterpretable -- no message, no error, no way to notice.

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


# --- A real approval, from a real pane, carrying its command (DEC-098) ------------------------
#
# The gate criterion of the 2026-09-19 plan, and the only place the ask-detail path meets a real
# agent. Everything else about it is fixtures shaped from captures.
#
# It has to drive a TUI in a tmux pane rather than `claude -p` / `codex exec`, because an
# approval is an interactive act: `codex exec` answers "This session does not permit approval
# escalation" and auto-rejects, so only `Stop` ever fires
# (`docs/acceptance-2026-08-29-codex-activity-detail.md`).
#
# Boundaries, the same ones both drills held: a disposable agent home, the owner's credential
# reached only through a symlink that is never opened here, hook and directory trust granted
# inside the disposable home through the TUI's own prompts (never `--dangerously-bypass-hook-
# trust`), a `remote-agents-test-*` tmux socket that is destroyed, and a spool made by the test.

_DRILL_SOCKET = "remote-agents-test-ask-detail"
_PANE_SETTLE_SECONDS = 2.0
_PANE_TIMEOUT_SECONDS = 120.0


def _tmux(*arguments: str, socket: str = _DRILL_SOCKET) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _pane_text(socket: str = _DRILL_SOCKET) -> str:
    return _tmux("capture-pane", "-p", "-t", "0", socket=socket).stdout


def _open(agent: str, ready: str, interstitials: tuple[Interstitial, ...]) -> None:
    """Answer whatever the pane shows until its composer is up (BL-105); fail if it cannot."""
    open_to_composer(
        _pane_text,
        lambda key: (_tmux("send-keys", "-t", "0", key), time.sleep(0.5)),
        ready=ready,
        interstitials=interstitials,
        agent=agent,
        timeout=_PANE_TIMEOUT_SECONDS,
    )


def _wait_for_pane(expected: str, *, socket: str = _DRILL_SOCKET) -> bool:
    """Poll the pane until it shows `expected`. Polled, never slept-then-asserted.

    A fixed sleep is what makes a drill like this flake: model latency is not a constant, and a
    sleep long enough to be safe makes the suite unusable.
    """
    deadline = time.monotonic() + _PANE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if expected in _pane_text(socket=socket):
            return True
        time.sleep(1.0)
    return False


def _type(text: str, *, socket: str = _DRILL_SOCKET) -> None:
    """Send a line, then Enter as its own key.

    Batched into one `send-keys` these drop during a redraw -- the failure recorded in the vault
    as *Batched tmux send-keys Drop During Redraw*.
    """
    _tmux("send-keys", "-t", "0", "-l", text, socket=socket)
    time.sleep(_PANE_SETTLE_SECONDS)
    _tmux("send-keys", "-t", "0", "Enter", socket=socket)


def _spooled_ask(spool: Path) -> AgentActivity | None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if spool.is_dir() and any(spool.iterdir()):
            for activity in drain_activity(spool):
                if activity.kind is ActivityKind.NEEDS_ANSWER:
                    return activity
        time.sleep(1.0)
    return None


def _live_pane_requirements(executable: str) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    for needed in (executable, "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")


@pytest.mark.live_profile
def test_a_real_codex_approval_spools_the_command_it_is_asking_about(tmp_path: Path) -> None:
    """One escalation in a real 0.154 pane, one record, and the command is in it."""
    _live_pane_requirements("codex")
    owner_auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    if not owner_auth.is_file():
        pytest.skip("BLOCKED: Codex is not logged in with ChatGPT")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = workspace / ".codex"
    codex_home.mkdir(mode=0o700)
    spool = tmp_path / "activity"
    # Never opened, copied or serialized here -- only linked, so the ordinary ChatGPT
    # entitlement is used instead of separate API billing.
    os.symlink(owner_auth, codex_home / "auth.json")
    (codex_home / "config.toml").write_text(
        'approval_policy = "on-request"\nsandbox_mode = "read-only"\n', encoding="utf-8"
    )
    install_agent_hooks(
        codex_home / "hooks.json",
        executable=Path(sys.executable),
        activity_directory=spool,
        provider="codex",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)

    _tmux("kill-server")
    try:
        _tmux(
            "new-session",
            "-d",
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            str(workspace),
            "-e",
            f"CODEX_HOME={codex_home}",
            "-e",
            f"{SESSION_ID_VARIABLE}={SessionId.new()}",
            "codex",
        )
        # Directory and hook trust are granted inside this disposable home only; Codex
        # persists both per home, so the owner's trust state is untouched.
        _open("codex", CODEX_READY, CODEX_OPENING)

        probe = tmp_path / "codex-probe.txt"
        _type(f"Run this exact shell command and nothing else: whoami > {probe}")
        if not _wait_for_pane("Would you like to run"):
            pytest.skip(f"BLOCKED: codex raised no approval: {_pane_text()[-600:]}")

        activity = _spooled_ask(spool)
        assert activity is not None, "a real escalation spooled no needs_answer"
        assert activity.detail is not None, "the ask arrived wordless"
        assert "whoami" in activity.detail, (
            f"the ask does not name the command it is about: {activity.detail!r}"
        )
        assert activity.ask == "Bash"
    finally:
        _tmux("kill-server")


@pytest.mark.live_profile
def test_a_real_claude_approval_spools_the_command_it_is_asking_about(tmp_path: Path) -> None:
    """The same proof for Claude, through the `PermissionRequest` event DEC-098 installs.

    Scoped to a disposable PROJECT rather than a disposable `CLAUDE_CONFIG_DIR`: a fresh config
    directory demands an interactive OAuth login, which a drill has no business performing. The
    settings file this installs is inside the temporary workspace and goes with it.
    """
    _live_pane_requirements("claude")

    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    spool = tmp_path / "activity"
    install_agent_hooks(
        workspace / ".claude" / "settings.local.json",
        executable=Path(sys.executable),
        activity_directory=spool,
    )

    _tmux("kill-server")
    try:
        _tmux(
            "new-session",
            "-d",
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            str(workspace),
            "-e",
            f"{SESSION_ID_VARIABLE}={SessionId.new()}",
            "claude",
        )
        _open("claude", CLAUDE_READY, CLAUDE_OPENING)

        # Into the mode that asks. Cycling is the only interface for this.
        for _ in range(5):
            if "manual mode" in _pane_text():
                break
            _tmux("send-keys", "-t", "0", "BTab")
            time.sleep(2.0)
        else:
            pytest.skip("BLOCKED: could not reach claude's manual mode")

        _type("Run the bash command: curl -s -o /dev/null -w '%{http_code}' https://example.com")
        if not _wait_for_pane("Do you want to proceed"):
            pytest.skip(f"BLOCKED: claude raised no approval: {_pane_text()[-600:]}")

        activity = _spooled_ask(spool)
        assert activity is not None, "a real approval spooled no needs_answer"
        assert activity.detail is not None, "the ask arrived wordless"
        assert "curl" in activity.detail, (
            f"the ask does not name the command it is about: {activity.detail!r}"
        )
        assert activity.ask == "Bash"
    finally:
        _tmux("kill-server")


# --- the opener itself, without an agent (BL-105) ---------------------------------------------
#
# Not live: these feed `open_to_composer` scripted screens, so they run everywhere and pin the
# one property the live drills cannot show on a good day -- that a screen the drill does not
# recognise FAILS, with the pane in the message, instead of skipping.


class _Screens:
    """A pane that shows each screen in turn, advancing when a key is pressed."""

    def __init__(self, *screens: str) -> None:
        self.screens = list(screens)
        self.pressed: list[str] = []

    def capture(self) -> str:
        return self.screens[0]

    def press(self, key: str) -> None:
        self.pressed.append(key)
        if key == "Enter" and len(self.screens) > 1:
            self.screens.pop(0)


def _fake_clock():
    now = [0.0]
    return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_the_opener_fails_on_a_screen_it_does_not_recognise_and_shows_it() -> None:
    clock, sleep = _fake_clock()
    screens = _Screens("Something new: pick a plan\n› 1. Pro\n  2. Free")

    # BaseException, then the type: a skip is also an outcome exception, and a skip here is
    # the exact defect this pins -- `pytest.raises(pytest.fail.Exception)` would let it through.
    with pytest.raises(BaseException) as failure:
        open_to_composer(
            screens.capture, screens.press, ready=CODEX_READY, interstitials=CODEX_OPENING,
            agent="codex", timeout=10, clock=clock, sleep=sleep,
        )  # fmt: skip

    assert failure.type is pytest.fail.Exception, f"it must FAIL, not {failure.type.__name__}"
    assert "Something new: pick a plan" in str(failure.value), "the pane must be in the failure"
    assert screens.pressed == [], "nothing may be typed into a screen the drill does not know"


def test_the_opener_answers_codex_0_155_s_rate_limit_prompt_by_its_words() -> None:
    """BL-105's screen: the option is chosen by name, wherever the highlight starts."""
    clock, sleep = _fake_clock()
    screens = _Screens(
        "Do you trust the contents of this directory?\n› 1. Yes, continue\n  2. No, quit",
        "Approaching rate limits\n› 1. Switch to gpt-6-mini\n  2. Keep current model\n"
        "  3. Keep current model (never show again)",
        "Hooks need review\n  1. Review\n› 2. Trust all and continue\n  3. Continue without",
        f"› {CODEX_READY}",
    )

    open_to_composer(
        screens.capture, screens.press, ready=CODEX_READY, interstitials=CODEX_OPENING,
        agent="codex", timeout=30, clock=clock, sleep=sleep,
    )  # fmt: skip

    assert screens.pressed == ["Enter", "Down", "Enter", "Enter"]


def test_the_opener_answers_claude_s_unnumbered_trust_prompt_from_a_real_capture() -> None:
    """Claude 2.1.280 lists `No, exit` first and numbers nothing; the choice is by words."""
    clock, sleep = _fake_clock()
    trust = (
        Path(__file__).resolve().parents[1] / "fixtures" / "panes" / "claude" / "dialog_trust.txt"
    ).read_text(encoding="utf-8")
    screens = _Screens(trust, f"  ⏵⏵ {CLAUDE_READY} on (shift+tab to cycle)")

    open_to_composer(
        screens.capture, screens.press, ready=CLAUDE_READY, interstitials=CLAUDE_OPENING,
        agent="claude", timeout=30, clock=clock, sleep=sleep,
    )  # fmt: skip

    assert screens.pressed == ["Down", "Enter"], "`Yes, I trust this folder` is the second line"
