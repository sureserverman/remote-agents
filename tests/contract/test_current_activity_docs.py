"""Current operator claims for the qualified Codex activity boundary."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CURRENT_ACTIVITY_DOCS = (
    _ROOT / "README.md",
    _ROOT / "docs" / "operator-runbook.md",
    _ROOT / "docs" / "profile-compatibility.md",
)


def test_current_docs_describe_the_qualified_codex_activity_boundary() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (_ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")
    runbook_lower = runbook.lower()
    current = "\n".join(path.read_text(encoding="utf-8") for path in _CURRENT_ACTIVITY_DOCS).lower()

    assert "install-agent-hooks --provider codex" in readme
    assert "install-agent-hooks --provider codex --remove" in runbook_lower
    assert "`/hooks`" in runbook
    assert "before trusting it" in runbook_lower
    assert "`stop` hook reports `completed`" in runbook_lower
    assert "`permissionrequest` hook reports `needs_answer`" in runbook_lower
    assert "content-free `action required` title" in runbook_lower
    assert "inferred `needs_answer`" in runbook_lower
    assert "telegram remains observation-only" in runbook_lower
    assert "does not claim rate- or output-limit notifications" in readme
    obsolete_claim = "|".join(
        (
            r"codex.*no " + "hook",
            r"no " + "hook system.*codex",
            r"codex, opencode.*no " + "hooks",
        )
    )
    assert not re.search(obsolete_claim, current)
