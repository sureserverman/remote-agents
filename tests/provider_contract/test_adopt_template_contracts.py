"""An `adopt_template` is what a rollover types into its successor, so it must be typeable there.

The rollover pass formats the successor's own provider's template with the handoff id and sends
it through the guarded send (DEC-099, DEC-115). A template that send would refuse fails every
rollover of that provider at the last step, after a successor was launched for nothing -- the
case a `/` command typed into Codex, which declares no command menu, would be. So each declared
template is held here to the send's own pre-paste rules against its own provider's composer.

And the rollable set is exactly the set of profiles that declare one, in both directions: a
rollable profile with no template has nothing to type, and a template on a profile left out of
the set is a provider that was ported and then never rolled over.
"""

from __future__ import annotations

from string import Formatter

from kit import drive_or_skip

from remote_agents.adapters.agents.registry import (
    adopt_templates,
    profiles_that_roll_over,
    provider_descriptors,
)
from remote_agents.domain.models import ProfileId
from remote_agents.ports.prompt_rules import pre_paste_refusal, prompt_text

_HANDOFF = "h-0123456789abcdef0123"


def test_adopt_template_contract(descriptor) -> None:
    """One field, the handoff id, and a single line the provider's own composer accepts."""
    template = drive_or_skip(descriptor, "adopt_template")
    fields = [
        (name, spec, conversion)
        for _, name, spec, conversion in Formatter().parse(template)
        if name is not None
    ]
    assert fields == [("handoff_id", "", None)], (
        f"{descriptor.profile_id}'s adopt template names {fields}; it may name the handoff id "
        "once, bare, and nothing else: an envelope supplies only that, and a conversion or a "
        "format spec would type something other than the id"
    )
    text = template.format(handoff_id=_HANDOFF)
    assert "\n" not in text
    assert prompt_text(text) == text, "the send would alter the template before pasting it"
    assert pre_paste_refusal(text, descriptor.composer) is None, (
        f"{descriptor.profile_id}'s guarded send refuses its own adopt template: "
        f"{pre_paste_refusal(text, descriptor.composer)}"
    )


def test_the_rollover_set_is_exactly_the_profiles_with_an_adopt_template() -> None:
    """A smoke check, not evidence: the set is built from the same map today. It guards a later
    change that derives either side some other way."""
    declared = {d.profile_id for d in provider_descriptors() if d.adopt_template is not None}
    assert profiles_that_roll_over() == frozenset(declared)
    assert set(adopt_templates()) == declared


def test_claude_and_codex_roll_over_with_their_own_adopt_templates() -> None:
    """The exact text, spelled once here: every other test reads it from the descriptors, so
    this is where a wrong vertical string is caught."""
    assert profiles_that_roll_over() == {ProfileId("claude"), ProfileId("codex")}
    assert adopt_templates() == {
        ProfileId("claude"): "/planning:executing-plans --adopt-handoff {handoff_id}",
        ProfileId("codex"): "$planning:executing-plans --adopt-handoff {handoff_id}",
    }
