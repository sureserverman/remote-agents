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

The second is why `UNKNOWN` is a member rather than a `None` return: an unrecognised ask is a
thing the surfaces must be able to *say* something about ("waiting for an answer"), and a
member forces every renderer to decide what that is instead of falling through a null check.
"""

from __future__ import annotations

import pytest

from remote_agents.ports.agent_activity import AskClass, ask_class


def test_ask_class_recognises_the_one_token_the_measurement_observed() -> None:
    """`Bash` is the only value four measured payloads ever carried."""
    assert ask_class("Bash") is AskClass.SHELL_COMMAND


@pytest.mark.parametrize(
    "token",
    ["Read", "Edit", "WebFetch", "SomeToolNobodyHasSeen", "", "bash", "BASH"],
    ids=["read", "edit", "webfetch", "unseen", "empty", "lowercase", "uppercase"],
)
def test_every_unrecognised_token_classifies_as_unknown_rather_than_leaking(token: str) -> None:
    """An unrecognised token is a class, not a string to pass through.

    Case included deliberately: `bash` and `BASH` are not `Bash`. Matching them would be
    guessing at a provider's conventions, and the honest answer to a token this project has
    not measured is that it does not know what it means — which is a thing the surfaces can
    say.
    """
    assert ask_class(token) is AskClass.UNKNOWN


def test_no_ask_is_not_an_unknown_ask() -> None:
    """`None` means the observation names no ask at all — a `completed`, or a Claude
    `needs_answer` whose provider sent prose instead. That is a different fact from an ask
    whose class is unrecognised, and collapsing them would have a surface say "waiting for an
    answer about something" on an observation that is not waiting for anything."""
    assert ask_class(None) is None
