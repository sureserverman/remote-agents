"""`local_context` composes the same conversation service the bot gets, with no secrets."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from backends import backend_for

from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.backend import Backend
from remote_agents.application.conversations import ConversationService


def test_the_context_carries_a_conversation_service_field() -> None:
    """On the backend now, which is where the bot's has always come from too."""
    assert "conversations" in Backend.__dataclass_fields__


def test_conversations_is_optional_so_a_host_without_one_still_starts() -> None:
    field = Backend.__dataclass_fields__["conversations"]
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
    """The TUI-parity widening, now carried by the backend both surfaces receive."""
    assert field_name in Backend.__dataclass_fields__


def test_no_further_capability_leaked_into_the_context() -> None:
    """The sealed surface widens only deliberately; anything unlisted here is scope creep.

    Two sets now, because the fields split in two and collapsing them back into one would
    lose the distinction the split was for. The **backend** carries what both surfaces
    drive; the **context** carries what is this surface's alone. A capability arriving on
    the wrong side is exactly what this test is for: `attach_argv` on the backend would
    make the bot inherit a route DEC-039 says is not its, and `conversations` on the
    context would put the resume service back to being composed twice.

    Growing either set is a decision, and this is where it is made visible.
    """
    surface = {
        "backend",
        # The profile narrowing this surface applies: `ProfileChoice` refuses any reason
        # alongside `available=True`, which the domain's `ProfileCompatibility` does not.
        "profiles",
        # DEC-039: deliberately not host-following, unlike the bot's.
        "attach_argv",
        # A parameter of `backend.capture`, not a capability of its own.
        "capture_redactions",
        # The console capabilities (DEC-040), wired only under console hosting.
        "open_in_console",
        "console_sync",
        "console_flash",
        "console_recovery",
    }
    shared = {
        "sessions",
        "projects",
        "conversations",
        "catalogue",
        "refresh_catalogue",
        "profiles",
        "capture",
        "activity_feed",
        "max_label_length",
    }

    assert set(TuiContext.__dataclass_fields__) == surface
    assert set(Backend.__dataclass_fields__) == shared
    assert surface & shared == {"profiles"}, (
        "`profiles` is the one name on both, and deliberately: the backend's is the domain "
        "`ProfileCompatibility` and the context's is this surface's `ProfileChoice`. Any "
        "other overlap is a capability composed twice."
    )


def test_a_none_conversations_context_is_constructible() -> None:
    context = TuiContext(
        backend=backend_for(
            sessions=object(),  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=tuple,
        ),
        profiles=(),
        attach_argv=lambda session_id: ("tmux",),
    )
    assert context.backend.conversations is None
    assert context.backend.capture is None


def test_a_wired_conversations_context_holds_the_service() -> None:
    service = ConversationService(object())  # type: ignore[arg-type]
    context = TuiContext(
        backend=backend_for(
            sessions=object(),  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=tuple,
            conversations=service,
        ),
        profiles=(),
        attach_argv=lambda session_id: ("tmux",),
    )
    assert context.backend.conversations is service
