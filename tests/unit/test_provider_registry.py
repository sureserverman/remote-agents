"""The provider registry: one descriptor per provider, capability absence declared.

The registry generalizes what `ProfileUsageReaders` already proved in miniature — a closed
mapping from provider identity to the objects that can answer for it — into the one table
composition reads per-provider capabilities from (ARCH-04). These tests pin the two facts a
drift would silently spend: the table covers exactly the curated provider set, and the
capability asymmetry between providers is *declared* (`None`), never invented (DEC-061).
"""

from __future__ import annotations

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.domain.profiles import closed_profiles


def test_exactly_one_descriptor_per_provider() -> None:
    descriptors = provider_descriptors()

    assert len(descriptors) == 4
    assert len({descriptor.profile_id for descriptor in descriptors}) == 4


def test_the_registry_covers_the_curated_provider_set_exactly() -> None:
    """The provider set, not the profile set: `claude-remote` is the claude binary.

    Derived from the curated table's executables rather than restated, so a fifth provider
    entering `domain/profiles.py` makes this test name the registry as the laggard.
    """
    providers = {definition.executable for definition in closed_profiles()}

    assert {str(descriptor.profile_id) for descriptor in provider_descriptors()} == providers


def test_capability_absence_is_declared_not_invented() -> None:
    """Cursor publishes no usage and takes no hooks; OpenCode takes no hooks (DEC-061)."""
    by_id = {str(descriptor.profile_id): descriptor for descriptor in provider_descriptors()}

    assert by_id["cursor-agent"].usage is None
    assert by_id["cursor-agent"].hooks is None
    assert by_id["opencode"].hooks is None
    assert by_id["claude"].usage is not None
    assert by_id["codex"].usage is not None
    assert by_id["opencode"].usage is not None
    assert by_id["claude"].hooks is not None
    assert by_id["codex"].hooks is not None


def test_every_provider_declares_a_session_source() -> None:
    """All four providers have a conversation catalogue today; a None here is a wiring gap."""
    assert all(descriptor.sessions is not None for descriptor in provider_descriptors())
