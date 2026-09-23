"""The documents say what an upgrade does to the running service: it restarts it (BL-104).

Until 0.48.0 `remote-agents upgrade` re-registered the daemon and said the running service "picks
up the new code" while the old process kept serving; an operator who believed the sentence
skipped the restart. Re-onboarding now restarts a running service and proves the new process
(DEC-102), so the documents describe that -- and none of them may still tell the operator to
restart by hand after an upgrade, or promise a pickup nothing performs.

Dated records (`docs/acceptance-*`, `docs/drill-*`) are history and are not swept.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _current_documents() -> list[Path]:
    documents = [_ROOT / "README.md", *sorted((_ROOT / "docs").glob("*.md"))]
    return [path for path in documents if not path.name.startswith(("acceptance-", "drill-"))]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = re.search(r"^#{1,3} ", rest, re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def test_the_upgrade_sections_say_the_running_service_is_restarted() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (_ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")

    for name, section in (
        ("README upgrade", readme[readme.index("remote-agents upgrade            #") :][:1500]),
        ("runbook § Upgrading", _section(runbook, "### Upgrading (DEC-057)")),
    ):
        assert "restart" in section, f"{name} does not say the service is restarted"
        assert "restarted: pid" in section, f"{name} does not show what a proved restart prints"


def test_no_current_document_leaves_the_restart_to_the_operator_after_an_upgrade() -> None:
    stale = re.compile(
        r"picks up the new code|restart[^.\n]{0,40}(by hand|manually)[^.\n]{0,40}after[^.\n]{0,20}"
        r"upgrad|before you restart the service",
        re.IGNORECASE,
    )
    offenders = [
        f"{path.relative_to(_ROOT)}:{number}"
        for path in _current_documents()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if stale.search(line)
    ]

    assert offenders == [], offenders
