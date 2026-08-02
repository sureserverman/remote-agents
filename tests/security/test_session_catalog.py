from pathlib import Path

from remote_agents.domain.conversations import ConversationSummary


def test_session_catalogue_adapters_do_not_read_transcript_content_or_expose_source_ids() -> None:
    root = Path(__file__).parents[2]
    sources = "\n".join(
        (root / "src" / "remote_agents" / "adapters" / "agents" / name).read_text(encoding="utf-8")
        for name in ("claude_sessions.py", "codex_sessions.py")
    )

    assert ".read_text(" not in sources
    assert "cmdline" not in sources
    assert "environ" not in sources
    assert "provider_conversation_id" not in ConversationSummary.__dataclass_fields__
