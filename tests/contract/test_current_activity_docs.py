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


#: Prose that would tell an operator OpenCode is not watched -- the claim, in the shapes it is
#: actually written in, since a promise is made in prose rather than in an identifier.
_UNWATCHED = re.compile(
    r"opencode[^.]{0,120}(no hooks|takes no hooks|unobserved|publish(es)? no"
    r"|nothing observes|reports? nothing|contributes? none)",
    re.IGNORECASE,
)

#: The **other** thing an agent can publish none of, and the reason this sweep needs a subject.
#: OpenCode publishes no rate limits -- truthfully, permanently, and the limits pane says so in
#: exactly the words above (`publish none, ever`). That sentence is not an activity claim, and
#: the sweep matched it the day the owner wrote it, turning a true document into a red suite.
_ABOUT_LIMITS = re.compile(r"limit", re.IGNORECASE)

#: The vocabulary that says a line IS about activity after all, which is what keeps the
#: exemption from swallowing the failure this sweep exists for: a line may name limits and
#: still make the claim, and then it is an offender.
_ABOUT_ACTIVITY = re.compile(
    r"hook|notif|activity|observ|watch|event|idle|completed|needs[_ ]answer", re.IGNORECASE
)


def _claims_matching(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    """Every sentence in one document matching `pattern`, with the line an operator would open.

    Flattening the newlines is what makes this a prose sweep rather than a line sweep, and the
    replacement is one character wide so every offset -- and therefore every reported line
    number -- is unchanged. A hard-wrapped document is the ordinary case here, not the corner.
    """
    flat = text.replace("\n", " ")
    return [
        (text.count("\n", 0, match.start()) + 1, _sentence_around(flat, match.start()))
        for match in pattern.finditer(flat)
    ]


def _sentence_around(text: str, position: int) -> str:
    """The sentence a match sits in, which is the unit a prose claim is actually made in."""
    start = text.rfind(".", 0, position) + 1
    end = text.find(".", position)
    return text[start : end if end != -1 else len(text)].strip()


def _calls_opencode_unwatched(text: str) -> list[tuple[int, str]]:
    """Every sentence in one document that tells an operator OpenCode is not watched.

    **Read by sentence, not by line, and both halves of that are load-bearing.** These
    documents are hard-wrapped, so a per-line sweep sees a claim only when it happens to fit
    on one line — which is the *weaker* of the two faults it caused: the subject of the
    sentence ("rate limits" or "activity") routinely sits on the line above the words that
    match, so a line-by-line reader can neither find a wrapped claim nor tell what a found one
    is about. Flattening the newlines keeps every offset identical, so a match still reports
    the line the operator would look at.
    """
    return [
        (number, sentence)
        for number, sentence in _claims_matching(text, _UNWATCHED)
        if not _ABOUT_LIMITS.search(sentence) or _ABOUT_ACTIVITY.search(sentence)
    ]


def test_current_docs_say_what_opencode_reports_and_what_it_never_will() -> None:
    """OpenCode joined the reporting providers on 2026-09-06, and the docs have to say so.

    This file exists because a document that still describes a retired capability sends an
    operator to wait for a notification that cannot arrive. The same failure has a mirror image,
    and this is it: a document that still calls a provider unwatched sends them to *not* install
    the thing that would notify them.

    Both halves again, for the reason the Codex case needed both. OpenCode reports two kinds, and
    its `completed` carries no closing sentence — not yet, but ever, because `session.idle`'s
    payload is one field and that field is a session id. An operator told only the first half
    will wait for words that are never coming, which is the exact complaint the sibling case
    above was written from.
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8").lower()
    runbook = (_ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8").lower()

    assert "install-agent-hooks --provider opencode" in readme
    assert "install-agent-hooks --provider opencode --remove" in runbook
    assert "no closing sentence" in readme + runbook, (
        "the negative half: an OpenCode completion is wordless permanently, not pending"
    )
    offenders = [
        f"{path.relative_to(_ROOT)}:{number}: {sentence}"
        for path in _CURRENT_ACTIVITY_DOCS
        for number, sentence in _calls_opencode_unwatched(
            path.read_text(encoding="utf-8")
        )
    ]
    assert offenders == [], (
        "a current document still calls opencode unwatched:\n" + "\n".join(offenders)
    )

    # **The exemption is asserted, not trusted.** A sweep that learns to ignore a subject can
    # ignore the failure it exists for, and nothing about a green run would show it -- so all
    # three controls are checked here, in the test that relies on them, rather than left to a
    # reader. The third is the wrap: the sentence that broke this sweep was split across two
    # lines, with its subject on the first and the matching words on the second.
    assert not _calls_opencode_unwatched(
        "The pane carries one row per agent that publishes rate limits at all -- today\n"
        "Claude and Codex; OpenCode and Cursor publish none, ever, so they get no row."
    ), "the limits sentence is being read as an activity claim again"
    assert _calls_opencode_unwatched(
        "OpenCode takes no hooks, so its rate limits are unknown too."
    ), "a real activity claim escaped by naming limits in the same breath"
    assert _calls_opencode_unwatched(
        "Nothing is installed for OpenCode, which\ntakes no hooks."
    ), "a claim wrapped across two lines is invisible again, which is how this sweep read past"


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
        r"pane[ -]quiet|quiet fallback|`quiet`"
        r"|no output since|stopped changing|profiles with no hook system",
        re.IGNORECASE,
    )
    # **`gone quiet` is ordinary English and had to be separated from the feature's own
    # vocabulary.** Reading by sentence found it wrapped across two lines in the symlink
    # troubleshooting section -- "if notifications have gone quiet with a healthy service,
    # check the path itself" -- which is a symptom an operator observes, not a promise this
    # project makes. The phrase counts only where the sentence also names the mechanism the
    # retired fallback used, which is what every real instance of the claim did.
    symptom = re.compile(r"gone quiet", re.IGNORECASE)
    mechanism = re.compile(r"pane|output|fallback|profile|hook|notification kind", re.IGNORECASE)

    # By sentence, for the reason the OpenCode sweep above is: these documents are
    # hard-wrapped, so `profiles with no hook system` -- one of the phrases this looks for, and
    # the one a close-out evaluator found by reading after a sweep read past it -- is more
    # likely to straddle two lines than to sit on one.
    offenders = [
        f"{path.relative_to(_ROOT)}:{number}: {sentence}"
        for path in swept
        for text in (path.read_text(encoding="utf-8"),)
        for number, sentence in (
            *_claims_matching(text, retired),
            *(
                claim
                for claim in _claims_matching(text, symptom)
                if mechanism.search(claim[1])
            ),
        )
    ]

    assert offenders == [], (
        "a current document still claims the pane-quiet fallback:\n" + "\n".join(offenders)
    )

    # The two controls the split above needs, for the reason the sibling sweep states: an
    # exemption nobody asserts is an exemption that can quietly swallow the failure.
    assert not [
        claim
        for claim in _claims_matching(
            "If notifications have gone quiet with a healthy service, check the path.", symptom
        )
        if mechanism.search(claim[1])
    ], "an operator's symptom is being read as this project's retired promise again"
    assert [
        claim
        for claim in _claims_matching(
            "For those profiles the session is reported to have gone quiet when its pane "
            "stops changing.",
            symptom,
        )
        if mechanism.search(claim[1])
    ], "the retired promise escaped by being phrased as a symptom"


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
