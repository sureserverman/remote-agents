"""What a from-scratch install actually ends up with, against a fabricated HOME (BL-099).

**This is the claim BL-099 is about, and it is the one no unit test can make.** The offer's own
tests inject a double for `install_agent_hooks`, so they prove the gates and nothing about what
lands on disk. Here the **real** installer runs against a real settings file, and the assertion
is `doctor`'s own reading afterwards — the same predicate the surfaces use, so "a fresh host can
report Claude limits" is answered end to end rather than at the seam.

The defect it pins: `install_agent_hooks` has exactly one caller in `src/` besides this offer —
the `install-agent-hooks` CLI command. `onboard` never called it, `upgrade` never did, and
`scripts/install.sh` never did. So a fresh host finished onboarding with a running service, a
registered daemon and a working console, and with the Claude limits row reading as *absent*:
honest under DEC-061, and indistinguishable from a provider that publishes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from remote_agents.bootstrap import _claude_limits_state
from remote_agents.composition import onboarding
from remote_agents.production import ProductionPaths


def _a_home_with_claude_but_no_hop(root: Path) -> Path:
    """A fabricated HOME carrying a Claude settings file and no hop.

    The settings file must exist: the installer **refuses to create** an agent configuration it
    did not find, which is deliberate — a host with no Claude is not a host to write a Claude
    configuration onto. That refusal is the very path `HookInstallError` travels, covered by the
    offer's own unit test.

    A `statusLine` of the operator's own is included because the interesting half of the wrap is
    that it *preserves* one; an empty document would prove only the easy case.
    """
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "bash /opt/mybar.sh"}}, indent=2),
        encoding="utf-8",
    )
    return home


def test_a_fresh_install_that_declines_reports_no_hop_and_writes_nothing(tmp_path: Path) -> None:
    """The `curl | bash` path, which is every installer run: nothing written, and doctor says so."""
    home = _a_home_with_claude_but_no_hop(tmp_path)
    before = (home / ".claude" / "settings.json").read_text(encoding="utf-8")

    installed = onboarding._offer_the_status_line_hop(home, interactive=False, assume_yes=False)

    assert installed is False
    assert (home / ".claude" / "settings.json").read_text(encoding="utf-8") == before, (
        "a non-interactive onboarding modified the operator's Claude settings"
    )
    paths = ProductionPaths.for_home(home)
    assert "not installed" in _claude_limits_state(paths), (
        "doctor did not report the hop as missing on a host that declined it"
    )


def test_a_fresh_install_that_accepts_ends_with_limits_readable(tmp_path: Path) -> None:
    """The whole of BL-099 in one assertion: yes at onboarding, and doctor then says installed.

    Driven through the **real** `install_agent_hooks` — no double — so this answers for what is
    written to disk rather than for the offer's gates. `_claude_limits_state` is `doctor`'s own
    function, so the fresh host is judged by exactly the reading an operator would get.
    """
    home = _a_home_with_claude_but_no_hop(tmp_path)

    installed = onboarding._offer_the_status_line_hop(
        home, interactive=True, assume_yes=False, confirm=lambda _prompt: True
    )

    assert installed is True
    paths = ProductionPaths.for_home(home)
    assert _claude_limits_state(paths) == "status-line hop installed", (
        "a host that accepted the offer still cannot report Claude limits"
    )

    # The operator's own status line survived the wrap -- the property DEC-051 asks of an
    # installer that has to remember what it used to own, checked on disk rather than trusted.
    document = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "/opt/mybar.sh" in document["statusLine"]["command"], (
        "the wrap discarded the operator's existing status line instead of carrying it"
    )
