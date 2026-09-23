"""The current documents describe the message relay the bot now has (DEC-099).

Until 0.47.0 the README said the bot "does not provide ... prompt relay" and "never relays
arbitrary ... agent text". Both were true, and both became false together. This sweep reads the
documents that make present-tense claims -- dated acceptance records stay as they were -- and
pins the retired sentences out and the owner-facing story in.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CURRENT = (
    _ROOT / "README.md",
    _ROOT / "docs" / "operator-runbook.md",
    _ROOT / "docs" / "architecture.md",
    _ROOT / "docs" / "profile-compatibility.md",
)
_RETIRED = re.compile(
    r"shell access, prompt relay|no prompt relay|never relays arbitrary", re.IGNORECASE
)


def test_relay_retired_no_relay_sentences_are_gone() -> None:
    hits = [
        f"{path.relative_to(_ROOT)}: {match.group(0)}"
        for path in _CURRENT
        for match in _RETIRED.finditer(path.read_text(encoding="utf-8"))
    ]

    assert hits == [], "a current document still says the bot relays nothing"


def test_relay_runbook_says_what_each_outcome_means_and_what_it_never_does() -> None:
    runbook = (_ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")
    section = runbook.split("## Sending a message to a session", 1)[1].split("\n## ", 1)[0]

    for words in ("sent", "queued", "not sent", "couldn't confirm", "cancel queued message"):
        assert words in section.lower(), f"the runbook's relay section never says {words!r}"
    assert "never does" in section and "`!`" in section and "dialog" in section


def test_relay_readme_scope_names_the_relay_and_its_decision() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    scope = readme.split("## Development", 1)[0]

    assert "DEC-099" in scope and "idle" in scope
    assert "**Send message**" in readme


def test_relay_architecture_names_the_port_the_adapter_asks() -> None:
    architecture = (_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "ports/message_relay.py" in architecture
    assert "check_telegram_actions.py" in architecture
