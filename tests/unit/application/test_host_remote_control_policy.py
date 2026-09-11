"""Direction and label policy for the host-level toggle, beside the pane toggle's own.

The same shape as `session_actions.remote_control_directions`, and deliberately a sibling
rather than a generalisation: the two have different subjects (a pane versus this machine),
so folding them together would mean one function whose argument means two things. What they
DO share is the vocabulary, and that sharing is asserted by identity here rather than by
two tables that happen to agree (DEC-007).
"""

from __future__ import annotations

import pytest

from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
    host_remote_control_directions,
    pair_available,
)
from remote_agents.application.session_actions import REMOTE_CONTROL_LABELS
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)

ACTIVE = RemoteControlState.ACTIVE
INACTIVE = RemoteControlState.INACTIVE

#: Written from the requirement: which direction is worth offering for each reading.
DIRECTIONS = {
    HostConnection.CONNECTED: (INACTIVE,),
    HostConnection.CONNECTING: (INACTIVE,),
    HostConnection.DISABLED: (ACTIVE,),
    HostConnection.DAEMON_ABSENT: (ACTIVE,),
    HostConnection.ERRORED: (ACTIVE, INACTIVE),
    HostConnection.UNREACHABLE: (ACTIVE, INACTIVE),
}

#: Pairing needs a live relay link to pair *to*.
PAIRABLE = {
    HostConnection.CONNECTED: True,
    HostConnection.CONNECTING: True,
    HostConnection.DISABLED: False,
    HostConnection.DAEMON_ABSENT: False,
    HostConnection.ERRORED: False,
    HostConnection.UNREACHABLE: False,
}


def status(connection: HostConnection) -> HostRemoteControlStatus:
    return HostRemoteControlStatus.observed(connection, server_name="Paisleys-Blender")


def test_the_policy_covers_every_connection_the_domain_can_report() -> None:
    """Exhaustive, so a sixth `HostConnection` fails here rather than rendering no button."""
    assert set(DIRECTIONS) == set(HostConnection)
    assert set(PAIRABLE) == set(HostConnection)


@pytest.mark.parametrize("connection", sorted(HostConnection))
def test_each_reading_offers_the_direction_that_is_worth_offering(
    connection: HostConnection,
) -> None:
    assert host_remote_control_directions(status(connection)) == DIRECTIONS[connection]


def test_an_errored_reading_offers_both_rather_than_guessing() -> None:
    """The same choice `remote_control_directions` makes for an unknown pane.

    Offering one direction on a guess is the failure the pane policy exists to avoid; a
    daemon that says "enabled, but the connection is errored" is exactly that case.
    """
    assert host_remote_control_directions(status(HostConnection.ERRORED)) == (ACTIVE, INACTIVE)


def test_a_host_with_no_capability_offers_nothing() -> None:
    """`None` is the declared absence a surface reads with `is None` (DEC-061/067).

    Returning `()` rather than raising lets a surface render the answer without first asking
    a second question it could disagree with -- the shape `remote_control_directions` uses.
    """
    assert host_remote_control_directions(None) == ()


@pytest.mark.parametrize("connection", sorted(HostConnection))
def test_pairing_is_offered_only_where_there_is_a_link_to_pair_to(
    connection: HostConnection,
) -> None:
    assert pair_available(status(connection)) is PAIRABLE[connection]


def test_pairing_is_not_offered_without_the_capability() -> None:
    assert pair_available(None) is False


def test_the_host_labels_are_its_own_table_because_the_pane_toggle_stopped_having_two() -> None:
    """The identity is gone, and its disappearance is the point rather than a regression.

    These two tables were deliberately *one object*: both surfaces once rendered "Remote
    Control on" / "Remote Control off" for both subjects, and re-stating two identical
    strings in two modules is the drift DEC-007 ends. That argument held only while the two
    vocabularies agreed. The pane toggle is one button now -- its direction is read from the
    pane at confirm time, so it has no "on" and no "off" to name -- while the host toggle
    still offers a direction per press (DEC-071 keeps it a sibling, not a generalisation).
    Keeping the alias would have forced one of those two truths to be spelled wrong.

    What replaces the identity is this test plus `test_every_offerable_direction_has_a_label`
    below: the host table must cover exactly the directions the host policy can offer, and
    must not borrow the pane's single word.
    """
    assert HOST_REMOTE_CONTROL_LABELS is not REMOTE_CONTROL_LABELS
    assert HOST_REMOTE_CONTROL_LABELS == {
        RemoteControlState.ACTIVE: "Remote Control on",
        RemoteControlState.INACTIVE: "Remote Control off",
    }
    assert set(HOST_REMOTE_CONTROL_LABELS).isdisjoint(REMOTE_CONTROL_LABELS)


def test_every_offerable_direction_has_a_label() -> None:
    """A direction a surface can offer and cannot name is a blank button."""
    offerable = {direction for directions in DIRECTIONS.values() for direction in directions}
    assert offerable <= set(HOST_REMOTE_CONTROL_LABELS)


def test_the_title_names_the_provider_because_the_subject_is_the_host() -> None:
    """`Remote Control` alone would read as the pane action the owner already knows."""
    assert HOST_REMOTE_CONTROL_TITLE == "Codex Remote Control"
