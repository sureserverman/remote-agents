"""What a descriptor declares about the folder-trust question its agent asks.

A `trust_dialog` is a capability like any other — `None` is a real answer (DEC-009): opencode
raises no such dialog, and declaring one for it would put a Trust button on a session whose
pane has no question on it, which then sends arrow keys into a live agent's prompt.

**The contract that earns this file is the cross-check, not the presence check.** `codex` and
`cursor-agent` draw the sentence *"Do you trust the contents of this directory?"* **verbatim**
— measured, both captures are in `fixtures/trust_dialogs/` and in
`docs/acceptance-2026-09-09-trust-dialogs.md`. So a dialog identified by its question is a
dialog that matches the wrong agent, and the confirming keypress is then computed from a row
layout that is not on screen: for codex the cursor rests on the affirmative and for claude on
the negative, so answering one with the other's arithmetic presses exactly the wrong option.
Every `identifies_by` is therefore checked against **every other agent's real capture**, not
merely against its siblings' declarations.
"""

from __future__ import annotations

import pathlib

import pytest
from kit import drive_or_skip

from remote_agents.adapters.agents.registry import provider_descriptors

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "trust_dialogs"

#: The recorded captures, by the profile they were taken from. Real panes, 120x40, each agent
#: launched into a directory it had never been asked about (Task 1.1).
_CAPTURES = {path.stem: path.read_text(encoding="utf-8") for path in _FIXTURES.glob("*.txt")}


def _dialogs() -> dict[str, object]:
    return {
        str(descriptor.profile_id): descriptor.trust_dialog
        for descriptor in provider_descriptors()
    }


def test_the_captures_this_contract_reads_are_the_ones_that_were_measured() -> None:
    """The fixtures are the evidence; a contract read against an empty directory proves nothing.

    Named first because every assertion below is vacuous without it: `glob` returning nothing
    makes each cross-check a loop over zero captures, which passes.
    """
    assert set(_CAPTURES) >= {"codex", "cursor-agent", "opencode", "claude"}, sorted(_CAPTURES)
    shared = "Do you trust the contents of this directory?"
    assert shared in _CAPTURES["codex"] and shared in _CAPTURES["cursor-agent"], (
        "the two agents that share their question verbatim no longer do, which is the premise "
        "this whole file is built on — re-read the acceptance document before relaxing anything"
    )


def test_every_descriptor_declares_a_complete_trust_dialog_or_none(descriptor) -> None:
    """Half a dialog is worse than none: it would classify a pane and then misread its rows.

    Through `drive_or_skip`, which is the kit's rule and not a preference: a contract test
    never writes its own `if x is None` branch, it asks the requirements table. A provider that
    declares no dialog skips here **by name**, visible in `pytest -rs`, and the None-ness itself
    is already asserted against the registry by `test_requirements_match_registry.py`. Writing
    the branch inline instead is what made this file's first version leave the kit's skip budget
    saying 9 while the run reported 8.
    """
    dialog = drive_or_skip(descriptor, "trust_dialog")
    for field in ("question", "affirmative", "negative", "cursor", "identifies_by"):
        assert getattr(dialog, field).strip(), f"{descriptor.profile_id}.{field} is empty"
    assert len(dialog.cursor) == 1, (
        f"{descriptor.profile_id}'s cursor is {dialog.cursor!r}; the parser looks for one "
        "character on a row, and a longer marker would not be found inside a boxed dialog"
    )


def test_no_two_providers_identify_themselves_by_the_same_string() -> None:
    declared = [
        (profile, dialog.identifies_by) for profile, dialog in _dialogs().items() if dialog
    ]
    identifiers = [identifier for _, identifier in declared]
    assert len(set(identifiers)) == len(identifiers), (
        f"two providers claim the same dialog: {declared}. One agent's parser would then "
        "answer the other's question with the wrong row arithmetic."
    )


@pytest.mark.parametrize("profile", sorted(_CAPTURES))
def test_each_identifier_appears_in_its_own_capture_and_in_no_others(profile: str) -> None:
    """The measured cross-check — every declaration against every real capture.

    `claude` is the one profile whose *own* capture cannot carry its identifier: this host sets
    `permissions.defaultMode: "auto"` and Claude Code 2.1.266 raises no dialog at all, so its
    strings are carried from 2.1.263 (acceptance document §5). The half that can be measured
    still is, and is the half that matters: its identifier must appear in nobody else's.
    """
    capture = _CAPTURES[profile]
    for other, dialog in _dialogs().items():
        if dialog is None or other == profile:
            continue
        assert dialog.identifies_by not in capture, (
            f"{other}'s dialog identifier {dialog.identifies_by!r} appears in {profile}'s real "
            f"capture, so {other}'s row arithmetic would be applied to {profile}'s screen"
        )
    own = _dialogs().get(profile)
    if own is not None and profile in {"codex", "cursor-agent"}:
        assert own.identifies_by in capture, (
            f"{profile} declares {own.identifies_by!r}, which is not in the capture that was "
            "measured from it — the dialog would never be recognised at all"
        )
