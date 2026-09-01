from pathlib import Path

from remote_agents.domain.conversations import ConversationSummary


def test_session_catalogue_adapters_keep_provider_ids_out_of_selection_metadata() -> None:
    root = Path(__file__).parents[2]
    # The moved sources, not the re-export shims left at the old paths — a sweep over a shim
    # asserts nothing about the code that actually runs.
    sources = "\n".join(
        (root / "src" / "remote_agents" / "adapters" / "agents" / provider / "sessions.py")
        .read_text(encoding="utf-8")
        for provider in ("claude", "codex")
    )

    assert ".read_text(" not in sources
    assert "cmdline" not in sources
    assert "environ" not in sources
    assert "provider_conversation_id" not in ConversationSummary.__dataclass_fields__


def test_no_conversation_summary_field_carries_a_provider_conversation_id() -> None:
    """Pin the boundary by type, not by the one field name that happened to breach it.

    The assertion above checks a *name*, which is what the docstring on
    `ConversationSummary` cites as enforcement of "the provider ID is not a field here".
    A field named anything else -- `origin: ProviderConversationId`, `source` -- would
    satisfy it untouched, so the claim was stronger than the check. This closes that gap:
    the annotation is what carries the type, and `from __future__ import annotations`
    means every one arrives here as a string.
    """
    offenders = [
        name
        for name, field in ConversationSummary.__dataclass_fields__.items()
        if "ProviderConversationId" in str(field.type)
    ]
    assert offenders == []
