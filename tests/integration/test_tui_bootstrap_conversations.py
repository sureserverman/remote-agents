"""`local_context` composes the same conversation service the bot gets, with no secrets."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.conversations import ConversationService


def test_the_context_carries_a_conversation_service_field() -> None:
    assert "conversations" in TuiContext.__dataclass_fields__


def test_conversations_is_optional_so_a_host_without_one_still_starts() -> None:
    field = TuiContext.__dataclass_fields__["conversations"]
    assert field.default is None


def test_local_context_composes_the_same_service_the_bot_composes() -> None:
    """Both compositions must build ConversationService from the same catalogue set.

    Read from the source rather than executed, because composing it for real needs a
    configured host; what matters here is that neither path invents its own catalogue.
    """
    source = Path("src/remote_agents/bootstrap.py").read_text(encoding="utf-8")
    assert source.count("ProfileConversationCatalogue(") == 1, (
        "the two surfaces must share one catalogue composition, not each build their own"
    )
    assert "conversations=conversations" in source


def test_local_context_needs_no_telegram_secrets() -> None:
    """The terminal is documented to run on a host with no Telegram credentials."""
    source = Path("src/remote_agents/bootstrap.py").read_text(encoding="utf-8")
    start = source.index("def local_context(")
    end = source.index("def _project_creator(")
    body = source[start:end]
    for forbidden in ("TelegramSecrets", "owner_user_id", "owner_chat_id", "bot_token"):
        assert forbidden not in body, f"local_context must not require {forbidden}"


def test_local_context_signature_is_unchanged_for_callers() -> None:
    from remote_agents.bootstrap import local_context

    parameters = list(inspect.signature(local_context).parameters)
    assert parameters == ["config", "connection", "paths"]


@pytest.mark.parametrize("field_name", ["capture", "conversations"])
def test_the_context_was_widened_by_exactly_the_two_planned_fields(field_name: str) -> None:
    assert field_name in TuiContext.__dataclass_fields__


def test_no_further_capability_leaked_into_the_context() -> None:
    """The sealed surface widens only deliberately; anything unlisted here is scope creep.

    `capture`/`conversations` were the TUI-parity widening (Stage 4 of that plan);
    `open_in_console` is the console-surface plan's Stage 2 widening — the composition
    wires it only under console hosting, and everywhere else the field stays None and the
    exec-attach contract is untouched. Growing this set is a decision, and this test is
    where it is made visible.
    """
    expected = {
        "launcher",
        "creator",
        "profiles",
        "refresh_catalogue",
        "attach_argv",
        "max_label_length",
        "catalogue",
        "capture",
        "capture_redactions",
        "conversations",
        "open_in_console",
        "console_sync",
    }
    assert set(TuiContext.__dataclass_fields__) == expected


def test_a_none_conversations_context_is_constructible() -> None:
    context = TuiContext(
        launcher=object(),  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(),
        refresh_catalogue=tuple,
        attach_argv=lambda session_id: ("tmux",),
    )
    assert context.conversations is None
    assert context.capture is None


def test_a_wired_conversations_context_holds_the_service() -> None:
    service = ConversationService(object())  # type: ignore[arg-type]
    context = TuiContext(
        launcher=object(),  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(),
        refresh_catalogue=tuple,
        attach_argv=lambda session_id: ("tmux",),
        conversations=service,
    )
    assert context.conversations is service
