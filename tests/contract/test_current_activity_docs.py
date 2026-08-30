"""Current operator claims for the qualified Codex activity boundary.

"Current" is the whole scope. `docs/acceptance-*.md` are deliberately outside it: they are dated
accounts of drills that happened, and one of them records observing the pane-quiet fallback on
2026-08-29. Editing those to match today's code would falsify an observation, so this sweep reads
only the documents that make claims in the present tense.
"""

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


def test_no_current_document_still_offers_the_retired_pane_quiet_fallback() -> None:
    """The fallback was retired on 2026-08-30; a document still promising it is a false claim.

    Swept as a whole-corpus regex rather than as a per-file assertion because the claim was
    spread across three documents and two registers -- a feature paragraph, a kinds table, a
    config upgrade note -- and the failure this closes is an operator reading one of them and
    expecting notifications for `opencode` that can no longer arrive.

    The dated acceptance records are excluded by construction: `_CURRENT_ACTIVITY_DOCS` and the
    architecture document below are the documents that speak in the present tense.
    """
    swept = (*_CURRENT_ACTIVITY_DOCS, _ROOT / "docs" / "architecture.md")
    # **The vocabulary AND the claim.** The first version of this swept only the retired
    # identifiers, which is what the Stage 1 gate remediation commit -- titled "name the concept,
    # not the deleted symbol" -- had just finished arguing was the wrong instrument. It passed
    # over a README paragraph describing the retired notification in full, hedge and all, without
    # once using a swept word: "for the profiles with no hook system -- its pane has produced no
    # output since a stated time, which is said as the guess it is." Two stages read past it, and
    # the close-out evaluator found it by reading rather than grepping. A promise is made in
    # prose, so the sweep has to look for the promise.
    retired = re.compile(
        r"gone quiet|pane[ -]quiet|quiet fallback|`quiet`"
        r"|no output since|stopped changing|profiles with no hook system",
        re.IGNORECASE,
    )

    offenders = [
        f"{path.relative_to(_ROOT)}:{number}: {line.strip()}"
        for path in swept
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if retired.search(line)
    ]

    assert offenders == [], (
        "a current document still claims the pane-quiet fallback:\n" + "\n".join(offenders)
    )


def test_current_docs_say_what_a_codex_notification_carries_and_what_it_does_not() -> None:
    """Both halves, because the asymmetry is the whole boundary and is easy to state as one.

    A Codex `Stop` now carries the agent's own last line, bounded exactly as Claude's is. A Codex
    `PermissionRequest` still carries nothing, and neither does the title-derived
    `needs_answer` -- and an operator who reads only the first half will expect a wordy approval
    notification that is never coming. The documents have to say which is which.
    """
    runbook = (_ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8").lower()
    readme = (_ROOT / "README.md").read_text(encoding="utf-8").lower()

    # Phrases chosen after checking they appear nowhere in either document, so this fails for
    # its own reason. A first draft asserted on `last_assistant_message`, "bounded" and
    # "carries no" and passed on arrival: the field name appears in an unrelated drill command
    # example, and the other two hit elsewhere for other reasons. A contract case satisfied by
    # coincidence pins nothing.
    assert "codex `stop` carries" in runbook, "the runbook must say the detail now arrives"
    assert "the agent's own last line" in runbook + readme
    assert "names no command" in runbook, (
        "the negative half: an approval notification is still wordless, and an operator who "
        "reads only the positive half will wait for words that are never coming"
    )


def test_the_retired_config_key_is_described_as_retired_rather_than_required() -> None:
    """The upgrade note told operators to add a key that is now tolerated, not required.

    That instruction was correct when written and is now the opposite of the truth: following it
    adds a key the schema ignores, and an operator who reads only the old paragraph believes a
    config without it will crash-loop. The runbook has to say which of the two it is.
    """
    runbook = (_ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")

    assert "activity_quiet_polls" in runbook, "silence is not the same as saying it was retired"
    assert "retired" in runbook.lower()
    assert "activity_quiet_polls = 3" not in runbook, (
        "the runbook still instructs the operator to add the retired key"
    )
