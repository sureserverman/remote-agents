"""`local_context` composes the same conversation service the bot gets, with no secrets."""

from __future__ import annotations

import ast
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
    """Both compositions take their conversation service from the one backend.

    Parsed rather than counted, and that is the point of this version. This test has now
    broken twice without the invariant ever being false: first it pinned the literal
    `conversations=conversations`, which described how the two compositions happened to be
    written when each built its own service; then it pinned
    `conversations=backend.conversations` twice, which described the moment after
    `compose_backend` arrived and before the boundary took the whole backend as one
    argument. Each time the spelling moved and the fact did not, and each time a green
    stage gate went red for a wiring that was correct.

    The fact is narrower than any of those spellings: `ProfileConversationCatalogue` and
    `_conversation_service` are each constructed exactly once in this module, and that once
    is inside `compose_backend`. If that holds, no composition can be carrying a second
    conversation service, however the one it has reaches it (DEC-019 — claim only what you
    check).
    """
    tree = ast.parse(Path("src/remote_agents/bootstrap.py").read_text(encoding="utf-8"))
    composer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "compose_backend"
    )

    def calls(scope: ast.AST, callee: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
        ]

    for callee in ("ProfileConversationCatalogue", "_conversation_service"):
        found = calls(tree, callee)
        assert len(found) == 1, (
            f"{callee} is constructed {len(found)} times; the two surfaces must share one "
            "composition rather than each build their own"
        )

    # `ProfileConversationCatalogue` is built inside `_conversation_service`, so the single
    # count above is the whole of its claim. The service itself is the one that has to be
    # reached from the shared composer, which is what makes both surfaces' copy the same.
    assert len(calls(composer, "_conversation_service")) == 1, (
        "the conversation service is composed outside compose_backend, so a surface can "
        "hold one the other never sees"
    )


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
    `open_in_console` is the console-surface plan's Stage 2 widening, `console_sync` its
    Stage 3 sibling, and `activity_feed`/`console_flash` its Stage 5 pair — a reader of the durable
    observation table, wired in every hosting since the feed is useful outside the
    console. The two console capabilities are wired only under console hosting; anywhere
    else those fields stay None and the exec-attach contract is untouched. Growing this
    set is a decision, and this test is where it is made visible.
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
        "activity_feed",
        "console_flash",
        # Sub-plan 3's addition: what the console's start-only repair did and could not do,
        # carried to the surface instead of printed. The composition root runs `settle()`
        # before Textual starts, so a `print` there is erased by the alternate screen
        # microseconds later — invisible for the whole session it describes.
        "console_recovery",
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
