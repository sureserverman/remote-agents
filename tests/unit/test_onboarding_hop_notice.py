"""Onboarding says whether the Claude status-line hop is installed (BL-099).

**The fact was already in the report and onboarding simply never said it.**
`_report_on_the_onboarded_host` prints `_doctor_report`'s JSON, which has carried
`claude_limits` since the hop shipped -- so this is not a new probe, a second predicate, or a
second place for the answer to drift. It is the existing answer, said in words, in the one
place a new operator is looking.

Why it matters enough to print: `install_agent_hooks` has exactly one call site in `src/` --
the `install-agent-hooks` CLI command itself. `onboard` did not call it until the offer landed
beside this notice; `upgrade` still does not and `scripts/install.sh` still does not. So a fresh
host finishes onboarding with a running service, a
registered daemon and a working console, and with the Claude limits row reading as *absent*.
That absence is honest -- DEC-061's "absent is a first-class answer" working exactly as
designed -- and therefore indistinguishable from a provider that genuinely publishes nothing.
`doctor` is the only artifact that says otherwise, and nothing prompts a new operator to run it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from remote_agents.composition import onboarding


def _report(claude_limits: str) -> dict[str, Any]:
    """A healthy-but-for-nothing doctor report carrying one `claude_limits` reading.

    `healthy: False` deliberately with **no degraded component**: the summary returns early on
    a healthy report, and this test is about the line that prints on the way past. Built from
    the shape `_doctor_report` actually emits rather than from a shape invented here -- the
    module's own comment at `onboarding.py:283-288` records a defect where a fixture supplied a
    `ready` key the product has never had, so the line under test never rendered and no test
    noticed.
    """
    return {"healthy": False, "components": {}, "claude_limits": claude_limits}


@pytest.fixture
def _paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive the summary without a real host: config readable, doctor report injected."""
    monkeypatch.setattr(onboarding, "describe_schema_drift", lambda _p: {"readable": True})
    monkeypatch.setattr(onboarding, "load_config", lambda _p: object())

    class _Paths:
        config_path = tmp_path / "config.toml"
        home = tmp_path

    return _Paths()


def _run(monkeypatch: pytest.MonkeyPatch, paths: Any, report: dict[str, Any]) -> int:
    import remote_agents.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "_doctor_report", lambda *_a, **_k: report)
    return onboarding._report_on_the_onboarded_host(paths, installed_daemon=True)


def test_onboarding_names_the_hop_when_it_is_not_installed(
    _paths: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The absent case is the one that needs words, and it names the command that fixes it."""
    _run(monkeypatch, _paths, _report("status-line hop not installed (run remote-agents ...)"))
    out = capsys.readouterr().out

    assert "install-agent-hooks" in out, (
        "onboarding finished without naming the command that makes Claude limits readable"
    )
    assert "Claude" in out and "limits" in out


def test_onboarding_says_nothing_about_the_hop_once_it_is_installed(
    _paths: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No line at all when it is installed -- a notice that always prints is not a notice.

    Asserted on the *human* line rather than on the whole output, because the doctor JSON
    directly above it contains `claude_limits` either way: a naive `"hop" not in out` would
    fail against the report it is printed beside, which is a test that can never pass rather
    than a defect in the product.
    """
    _run(monkeypatch, _paths, _report("status-line hop installed"))
    human = [
        line
        for line in capsys.readouterr().out.splitlines()
        if not line.startswith("{") and "install-agent-hooks" in line
    ]

    assert human == [], f"onboarding nagged about a hop that is already installed: {human}"
