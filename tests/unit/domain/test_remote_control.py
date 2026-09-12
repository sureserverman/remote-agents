"""Domain pins for `RemoteControlDefault` — the stored intention, beside the observed state.

`test_host_remote_control.py` next door pins the host snapshot and its derivation. This file
pins the type that is *not* a reading: what the owner has asked for next time. The two are
separate types on purpose and the point of most of these assertions is that nothing lets them
be used for each other.
"""

from __future__ import annotations

import pytest

from remote_agents.domain.remote_control import RemoteControlDefault, RemoteControlState


def test_there_are_three_states_with_stable_stored_values() -> None:
    """The values are written into the owner's settings file, so they are not free to change.

    Pinned as literals rather than derived from the members: a renamed value would be read back
    as unknown from a file written by the previous version, which reads to the owner as their
    choice having been forgotten.
    """
    assert [member.value for member in RemoteControlDefault] == ["on", "off", "provider_default"]


def test_the_provider_default_is_its_own_member_not_an_absence() -> None:
    """It is a thing the owner can choose, so it must be a value that can be held and compared.

    The alternative design -- `None` for "not set" -- is what this asserts against: every
    caller would then need a branch for a missing value, and the one that forgot it would
    render *off*, which the premise check measured as the opposite of what the pane does
    (`docs/acceptance-2026-09-11-surface-refresh.md` section 8).
    """
    assert RemoteControlDefault.PROVIDER_DEFAULT is not None
    assert RemoteControlDefault("provider_default") is RemoteControlDefault.PROVIDER_DEFAULT


@pytest.mark.parametrize("member", list(RemoteControlDefault))
def test_no_default_is_equal_to_any_observed_state(member: RemoteControlDefault) -> None:
    """A stored intention and a live reading must not compare equal through `StrEnum`.

    Both are `StrEnum`s and both have members spelled `on`-ish, so `==` between them is a
    mistake that would otherwise pass silently: `RemoteControlState.ACTIVE` is `"active"` and
    this type's `ON` is `"on"`, and the one pair that could have collided is asserted here
    rather than assumed. A surface holding the wrong one of the two would act on a pane using a
    value that describes a file.
    """
    assert all(member != state for state in RemoteControlState)
    assert member.value not in {state.value for state in RemoteControlState}


def test_an_unknown_stored_value_raises_rather_than_resolving() -> None:
    """The *adapter* forgives an unknown value by answering the default; the type must not.

    Total reading is a property of the boundary that touches the file
    (`ports/remote_control_default.py`), and it is implemented by catching this. A type that
    silently resolved an unknown string would move that decision somewhere no test is looking.
    """
    with pytest.raises(ValueError):
        RemoteControlDefault("sometimes")
