"""No shared module may hold the words a surface says (DEC-043).

The rule is old and this file is new, and the reason it is new is worth writing down: the
plan that added the ask class tried to assert this rule with a grep three times, and a grep
cannot tell a module that *says* a sentence from a module that *explains why it must not*.

    grep -rn 'waiting for an answer about' src/remote_agents/application/ src/remote_agents/ports/

That command hit `ask_class`'s own docstring — the paragraph arguing that collapsing "no ask"
into "unknown ask" would make a surface say "waiting for an answer about something" on an
observation that is not waiting for anything. The prose was correct, the code was correct, and
the check failed. It was the third time in one plan that a text search matched the discussion
of the thing it was searching for, so the answer is to stop searching text.

**What this asserts instead is structure.** A shared module may define the *classes* — the
vocabulary both surfaces reason about — and may not define a mapping from one of those classes
to owner-facing prose. That is exactly the line DEC-043 draws: the decision is shared, the
sentence stays the surface's, because a chat message and a 73-column table row are not sized
the same and a shared renderer is how one surface's wording quietly becomes the other's.

A docstring can say "shell command" as often as it likes; a `dict[AskClass, str]` in
`ports/` or `application/` is the defect.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import remote_agents.application
import remote_agents.ports
from remote_agents.ports.agent_activity import AskClass

SHARED_PACKAGES = (remote_agents.ports, remote_agents.application)


def _modules():
    for package in SHARED_PACKAGES:
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
            yield importlib.import_module(info.name)


def _ask_prose_maps(module) -> list[str]:
    """Attributes mapping an `AskClass` to a string — this surface's wording, in a shared home.

    **Scoped to `AskClass`, and the narrowing is deliberate.** The first draft of this check
    flagged any `dict[Enum, str]` in `ports/` or `application/` and found five that predate
    this work — `session_actions._EXPLANATIONS`, `session_views._EMOJI_OF_GROUP`, and three
    `REMOTE_CONTROL_LABELS`. They are not obviously defects: DEC-043 as this repo applies it
    is not a blanket ban on shared strings (`session_views.limit_lines` returns sentences and
    cites DEC-043 while doing so); the ban is stated per-module, most sharply in
    `notification_policy`, whose docstring says it may return a signal and never a sentence.

    So a check declaring all five violations would be asserting a rule this project does not
    hold, and would fail forever on code nobody intends to change — which is worse than no
    check. What this plan can defend is its own vocabulary: nothing shared turns an `AskClass`
    into words. The wider question is left where it was found, unasserted, rather than
    smuggled in behind a test.
    """
    found = []
    for name in dir(module):
        if name.startswith("__"):
            continue
        value = getattr(module, name, None)
        if not isinstance(value, dict) or not value:
            continue
        if any(isinstance(key, AskClass) for key in value):
            found.append(f"{module.__name__}.{name} = {value!r}")
    return found


def test_no_shared_module_turns_an_ask_class_into_owner_facing_words() -> None:
    """Swept over every module in `ports/` and `application/`, not the two this plan touched.

    The point of a structural check is that it covers the modules nobody has written yet.
    """
    offenders = [entry for module in _modules() for entry in _ask_prose_maps(module)]
    assert not offenders, (
        "a shared module holds a surface's wording for an ask class (DEC-043); move it into "
        "the adapter that says it:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("member", list(AskClass), ids=lambda m: m.value)
def test_each_ask_class_is_worded_by_each_surface_or_deliberately_by_neither(
    member: AskClass,
) -> None:
    """Both surfaces answer for every class, and `UNKNOWN`'s answer is silence in both.

    This is the half a structural ban cannot give: knowing that nobody shares the words says
    nothing about whether anybody has any. `UNKNOWN` is absent from both maps on purpose — an
    unrecognised ask contributes no clause — and asserting that here is what stops a later
    edit adding "about something" to one surface and not the other.
    """
    from remote_agents.adapters.telegram.notifications import _ASK_WORDS
    from remote_agents.adapters.tui.screens.feed import ASK_WORDS

    if member is AskClass.UNKNOWN:
        assert member not in _ASK_WORDS and member not in ASK_WORDS
        return
    assert member in _ASK_WORDS, f"the bot has no words for {member}"
    assert member in ASK_WORDS, f"the feed has no words for {member}"
