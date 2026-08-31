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
    """Cursor takes no hooks and OpenCode takes no hooks — declared None (DEC-061).

    Cursor's `usage` is deliberately NOT None: its reader answers "I publish nothing",
    which renders as "not reported by this agent" — the permanent, honest sentence.
    A None here would drop cursor from the fold and every cursor session would render
    "no conversation matched yet", the temporary state, forever. Found live at the
    Stage 2 gate; the sibling test below pins the rendering-relevant consequence.
    """
    by_id = {str(descriptor.profile_id): descriptor for descriptor in provider_descriptors()}

    assert by_id["cursor-agent"].usage is not None
    assert by_id["cursor-agent"].hooks is None
    assert by_id["opencode"].hooks is None
    assert by_id["claude"].usage is not None
    assert by_id["codex"].usage is not None
    assert by_id["opencode"].usage is not None
    assert by_id["claude"].hooks is not None
    assert by_id["codex"].hooks is not None


def test_the_fold_keeps_cursor_answering_rather_than_absent(tmp_path) -> None:
    """DEC-061's two sentences stay apart through the registry fold.

    `read` returning an empty `AgentUsage` and `read` returning `None` render differently
    ("not reported by this agent" vs "no conversation matched yet"), so the composed
    reader set must still *match* a cursor session and answer emptily. `limits` likewise
    keeps cursor's windowless entry, so a quiet provider stays distinguishable from a
    dropped one.
    """
    from datetime import UTC, datetime

    from remote_agents.adapters.agents.registry import usage_readers
    from remote_agents.domain.models import ProfileId
    from remote_agents.ports.agent_usage import UsageQuery

    readers = usage_readers(provider_descriptors())
    answer = readers.read(
        UsageQuery(ProfileId("cursor-agent"), tmp_path, datetime.now(UTC), None)
    )

    assert answer is not None, "cursor must match a reader, not fall off the table"
    assert answer.is_empty
    assert [str(limits.profile_id) for limits in readers.limits()] == [
        "claude",
        "codex",
        "opencode",
        "cursor-agent",
    ]


def test_every_provider_declares_a_session_source() -> None:
    """All four providers have a conversation catalogue today; a None here is a wiring gap."""
    assert all(descriptor.sessions is not None for descriptor in provider_descriptors())
