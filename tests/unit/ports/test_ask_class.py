"""What an agent is waiting on, as a class this project chose rather than a token it was sent.

`ask` arrives from a provider as its own token for a tool — `Bash`. Two things must be true of
it before a surface can put it in front of the owner:

* **The token is never the words.** DEC-067's whole argument, and DEC-074's after it: a
  provider token is not a sentence, and rendering one where a sentence belongs conflates two
  kinds of string. So the token is classified here and worded by each surface (DEC-043).
* **The mapping is total.** `docs/acceptance-2026-08-29-codex-activity-detail.md` records
  `tool_name` as observed *only* as `Bash` across all four measured payloads, and says
  explicitly that its value space is unverified beyond that instance. A mapping that assumed
  otherwise would render whatever a provider sent the day it sent something new.

**`bash` joined `Bash` on 2026-09-06, and the rule that admitted it is the same rule that kept
it out before.** The earlier version of this file pinned lowercase `bash` as UNKNOWN with the
reason "matching it would be guessing at a provider's conventions" — which was exactly right
while no provider had been measured sending it. `docs/acceptance-2026-09-06-opencode-activity.md`
measured OpenCode sending `properties.permission` as `"bash"`, so the token stopped being a
guess and became the second observed spelling of one class. `BASH`, and every other casing,
stays unrecognised for the unchanged reason: nobody has seen one.

The second is why `UNKNOWN` is a member rather than a `None` return: an unrecognised ask is a
thing the surfaces must be able to *say* something about ("waiting for an answer"), and a
member forces every renderer to decide what that is instead of falling through a null check.
"""

from __future__ import annotations

import pytest

from remote_agents.ports.agent_activity import AskClass, ask_class


def test_ask_class_recognises_the_tokens_the_measurements_observed() -> None:
    """One per provider, each from its own capture, and nothing either side of them."""
    assert ask_class("Bash") is AskClass.SHELL
    assert ask_class("bash") is AskClass.SHELL


@pytest.mark.parametrize(
    "token",
    ["Read", "Edit", "WebFetch", "SomeToolNobodyHasSeen", "", "BASH", "Bash_"],
    ids=["read", "edit", "webfetch", "unseen", "empty", "uppercase", "suffixed"],
)
def test_every_unrecognised_token_classifies_as_unknown_rather_than_leaking(token: str) -> None:
    """An unrecognised token is a class, not a string to pass through.

    Case included deliberately: `bash` and `BASH` are not `Bash`. Matching them would be
    guessing at a provider's conventions, and the honest answer to a token this project has
    not measured is that it does not know what it means — which is a thing the surfaces can
    say. `bash` left this list on 2026-09-06 by being measured, not by being guessed; `BASH`
    is still here, and would leave the same way or not at all.
    """
    assert ask_class(token) is AskClass.UNKNOWN


def test_no_ask_is_not_an_unknown_ask() -> None:
    """`None` means the observation names no ask at all — a `completed`, or a Claude
    `needs_answer` whose provider sent prose instead. That is a different fact from an ask
    whose class is unrecognised, and collapsing them would have a surface say "waiting for an
    answer about something" on an observation that is not waiting for anything."""
    assert ask_class(None) is None


def test_no_generated_token_but_the_measured_ones_are_ever_recognised() -> None:
    """A generated sweep, because the value space is precisely what is unknown here.

    The hand-written cases above name tokens someone thought of. This one covers the shape
    `_plain_token` admits — `[A-Za-z0-9_-]{1,64}` — across a deterministic spread of it, so the
    claim "only `Bash` is recognised" is made over the admissible set rather than over a list.
    Deterministic rather than random: a check that fails one run in fifty is a check nobody
    trusts, and this project already has a rule about tests that pass for reasons other than
    the one they name.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    generated = {
        alphabet[index % len(alphabet)] * (1 + index % 8) + str(index) for index in range(600)
    }
    measured = {"Bash", "bash"}
    generated |= {"Bash".upper(), "Bas", "Bashh", " Bash", "Bash "} | measured
    for token in generated:
        if token in measured:
            continue
        assert ask_class(token) is AskClass.UNKNOWN, f"{token!r} was recognised"
    for token in measured:
        assert ask_class(token) is AskClass.SHELL
