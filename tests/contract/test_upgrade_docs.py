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


_STALE = re.compile(
    r"picks up the new code|restart[^.]{0,40}(by hand|manually)[^.]{0,40}after[^.]{0,20}upgrad"
    r"|before you restart the service",
    re.IGNORECASE,
)


def _stale_passages(text: str) -> list[int]:
    """The line each stale phrasing starts on, read across line wraps and blockquote markers.

    Prose wraps at a fixed width, so a sentence can split anywhere -- a line-by-line match only
    catches the ones that happened not to. The text is folded to one line with a map back to where
    each piece began.
    """
    folded, starts = [], []
    for number, line in enumerate(text.splitlines(), 1):
        piece = re.sub(r"^\s*(?:>\s*)?", "", line).strip()
        starts.append((sum(len(part) + 1 for part in folded), number))
        folded.append(piece)
    joined = " ".join(folded)
    return [
        max(number for offset, number in starts if offset <= match.start())
        for match in _STALE.finditer(joined)
    ]


def test_no_current_document_leaves_the_restart_to_the_operator_after_an_upgrade() -> None:
    offenders = [
        f"{path.relative_to(_ROOT)}:{number}"
        for path in _current_documents()
        for number in _stale_passages(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], offenders


def test_a_stale_phrase_split_by_a_line_wrap_is_still_found() -> None:
    """The mutant for the sweep itself: what a line-by-line match missed."""
    wrapped = "Intro line.\n\nThen re-run onboarding so the daemon picks up\n> the new code.\n"

    assert _stale_passages(wrapped) == [3]
    assert _stale_passages("Nothing stale here.\n") == []
