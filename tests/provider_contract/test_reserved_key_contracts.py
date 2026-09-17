"""What a descriptor declares about the keys its own agent has already taken.

The F-key console binds eleven function keys as tmux **root** bindings on its own socket, and
a root binding takes that key from every pane on that server. OpenCode binds `F2`
(`model_cycle_recent`, opencode.ai/docs/keybinds; Shift+F2 is the reverse, and its leader is
Ctrl+X) -- so a console that bound `F2` for itself would silently remove a keybind from a live
agent. Claude Code, Codex and `cursor-agent` bind no function key at all (`cursor-agent --help`
on 2026.09.10 names none and `~/.cursor` carries none), so their panes lose nothing.

`reserved_keys` is how that reaches the forwarding script, and the contracts here are what keep
it the *only* way it reaches it (DEC-070): the provider that binds the key declares it in its own
package, and the console reads the registry rather than carrying a list of its own.

**Driven unconditionally, like `test_identity_contracts.py` and unlike the capability
contracts.** There is no requirements row for `reserved_keys` and there must not be one: the
table's three states describe a capability that can genuinely be absent (DEC-061), and an empty
reservation is not an absence -- it is a provider answering *I reserve nothing*. Every provider
therefore has an answer here, and every answer is driven.
"""

from __future__ import annotations

import re

from requirements import DECLARATIONS

from remote_agents.adapters.agents.registry import provider_descriptors, reserved_keys_by_profile
from remote_agents.ports.provider_descriptor import capability_fields

#: tmux's spelling of a function key, which is the spelling this field carries: capital `F`,
#: `F1` through `F12`. Textual spells the same keys `f1`..`f12` and the TUI uses that form; the
#: two live in one codebase, so the contract pins which one a descriptor declares.
_TMUX_FUNCTION_KEY = re.compile(r"^F(?:[1-9]|1[0-2])$")

#: What was measured, per provider, and therefore what may be asserted. A provider the registry
#: gains later is still driven by every contract below -- only this pin names names, and it is
#: applied to the providers it was measured for rather than to whatever the registry holds.
_MEASURED: dict[str, frozenset[str]] = {
    "claude": frozenset(),
    "codex": frozenset(),
    "opencode": frozenset({"F2"}),
    "cursor-agent": frozenset(),
}


def test_the_descriptor_declares_a_set_of_key_names(descriptor) -> None:
    """A frozenset of strings, always -- `None` is not one of this field's answers."""
    reserved = descriptor.reserved_keys
    assert isinstance(reserved, frozenset), (
        f"{descriptor.profile_id} declares {type(reserved).__name__} reserved keys; the field "
        "is a frozenset so that 'reserves nothing' is an empty set and never a None"
    )
    assert all(isinstance(key, str) for key in reserved), (
        f"{descriptor.profile_id} declares a non-string key name: {sorted(map(repr, reserved))}"
    )


def test_every_reserved_key_is_spelled_the_way_tmux_spells_it(descriptor) -> None:
    """`F2`, not `f2`: this value is handed to `tmux bind-key`, not to Textual.

    The two spellings meet in this codebase -- the TUI's bindings are lowercase -- and tmux does
    not resolve the other one, so a lowercase declaration would not fail loudly. It would bind
    nothing and leave the key stolen from the pane it was supposed to reach.
    """
    for key in sorted(descriptor.reserved_keys):
        assert _TMUX_FUNCTION_KEY.match(key), (
            f"{descriptor.profile_id} reserves {key!r}, which is not a tmux function-key name; "
            "the console binds F1..F12 and this field names them in tmux's spelling"
        )


def test_the_reservation_is_not_a_capability_anywhere_in_the_kit() -> None:
    """No requirements row, by design -- and this is what keeps one from being added.

    `requirements.py`'s SUPPORTED/UNSUPPORTED/CONDITIONAL table exists to describe capabilities
    whose absence is a declared `None` (DEC-061). A reservation has no such absence: three of the
    four providers declare `frozenset()`, and that is an answer they were measured for, not a
    capability they lack. Declaring it UNSUPPORTED would turn three real answers into three
    skips, and SUPPORTED would make `frozenset()` a contradiction.
    """
    assert "reserved_keys" not in capability_fields(), (
        "`reserved_keys` reads as a capability to `capability_fields()`, which drives the "
        "requirements table; an empty reservation is an answer, not a declared absence"
    )
    for profile, row in DECLARATIONS.items():
        assert "reserved_keys" not in row, (
            f"{profile} declares a requirements state for `reserved_keys`; the table is for "
            "capabilities that can be absent, and this field's empty value is present"
        )


def test_the_provider_that_binds_a_function_key_is_the_one_that_declares_it() -> None:
    """The measurement, pinned: `opencode` reserves `F2` and the other three reserve nothing."""
    declared = {
        str(descriptor.profile_id): descriptor.reserved_keys
        for descriptor in provider_descriptors()
    }
    assert set(_MEASURED) <= set(declared), (
        f"the registry no longer holds every measured provider: {sorted(declared)}"
    )
    for profile, expected in _MEASURED.items():
        assert declared[profile] == expected, (
            f"{profile} declares {sorted(declared[profile])}, measured {sorted(expected)}; a "
            "reservation is a reading of the agent's own keybinds, so change the measurement "
            "and this pin together"
        )


def test_the_registry_answers_for_every_curated_profile() -> None:
    """`reserved_keys_by_profile()` is total, and a profile reserving nothing maps to an empty set.

    Total for the reason `profile_glyphs` is: the caller composes a forwarding script for
    whatever profile a pane is running, and a lookup that could raise or miss would turn one
    unrecognised profile into either an exception or a key silently stolen.
    """
    from remote_agents.domain.profiles import closed_profiles

    by_profile = reserved_keys_by_profile()
    curated = {str(profile.profile_id) for profile in closed_profiles()}
    assert curated, "the curated profile set is empty; this contract would assert nothing"
    assert set(by_profile) == curated, (
        f"answered for {sorted(by_profile)}, curated {sorted(curated)}"
    )
    for profile, reserved in by_profile.items():
        assert isinstance(reserved, frozenset), f"{profile} answered {type(reserved).__name__}"
    assert by_profile["opencode"] == frozenset({"F2"})
    assert all(
        by_profile[profile] == frozenset() for profile in ("claude", "codex", "cursor-agent")
    )


def test_the_registry_answers_nothing_for_a_profile_no_vertical_declares() -> None:
    """An unrecognised profile reserves nothing, rather than raising at a composition site."""
    from remote_agents.domain.models import ProfileId

    by_profile = reserved_keys_by_profile()

    assert by_profile.get(str(ProfileId("claude-remote")), frozenset()) == frozenset()
