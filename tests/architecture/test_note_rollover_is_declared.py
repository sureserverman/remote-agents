"""Which providers may roll over with a note is each vertical's declared fact (DEC-122).

Claude and Codex are the two whose agents can be asked to write a note and then read one
(Sub-plan 5 Task 2.1); OpenCode and Cursor declare `False`, so neither surface offers the
action on their sessions.
"""

from __future__ import annotations

import dataclasses
import typing


def test_only_claude_and_codex_declare_the_note_rollover() -> None:
    from remote_agents.adapters.agents.registry import provider_descriptors

    declared = {str(d.profile_id): d.note_rollover for d in provider_descriptors()}
    assert declared == {"claude": True, "codex": True, "opencode": False, "cursor-agent": False}


def test_the_note_rollover_flag_is_a_plain_bool_defaulting_to_off() -> None:
    """A flag, not a capability: `False` is the declared answer, and there is no `None`."""
    from remote_agents.ports.provider_descriptor import ProviderDescriptor, capability_fields

    hints = typing.get_type_hints(ProviderDescriptor)
    assert hints["note_rollover"] is bool
    assert ProviderDescriptor.__dataclass_fields__["note_rollover"].default is False
    assert "note_rollover" not in capability_fields()
    assert dataclasses.is_dataclass(ProviderDescriptor)


def test_the_composition_offers_the_note_rollover_on_the_declared_profiles() -> None:
    from remote_agents.adapters.agents.registry import profiles_that_roll_over_with_a_note
    from remote_agents.domain.models import ProfileId

    assert profiles_that_roll_over_with_a_note() == {ProfileId("claude"), ProfileId("codex")}
